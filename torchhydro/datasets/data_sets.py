"""
Author: Wenyu Ouyang
Date: 2022-02-13 21:20:18
LastEditTime: 2023-07-31 15:15:17
LastEditors: Wenyu Ouyang
Description: A pytorch dataset class; references to https://github.com/neuralhydrology/neuralhydrology
FilePath: /torchhydro/torchhydro/datasets/data_sets.py
Copyright (c) 2021-2022 Wenyu Ouyang. All rights reserved.
"""
import logging
import sys
from typing import Optional
import numpy as np
import torch
from torch.utils.data import Dataset
from tqdm import tqdm
import pint_xarray
from hydrodataset import HydroDataset
from torchhydro.datasets.data_scalers import ScalerHub, unify_streamflow_unit, wrap_t_s_dict
import xarray as xr

LOGGER = logging.getLogger(__name__)


def _fill_gaps_da(da: xr.DataArray, fill_nan: Optional[str] = None) -> xr.DataArray:
    """Fill gaps in a DataArray"""
    if fill_nan is None or da is None:
        return da
    assert isinstance(da, xr.DataArray), "Expect da to be DataArray (not dataset)"
    # fill gaps
    if fill_nan == "et_ssm_ignore":
        all_non_nan_idx = []
        for i in range(da.shape[0]):
            non_nan_idx_tmp = np.where(~np.isnan(da[i].values))
            all_non_nan_idx = all_non_nan_idx + non_nan_idx_tmp[0].tolist()
        # some NaN data appear in different dates in different basins
        non_nan_idx = np.unique(all_non_nan_idx).tolist()
        for i in range(da.shape[0]):
            targ_i = da[i][non_nan_idx]
            da[i][non_nan_idx] = targ_i.interpolate_na(
                dim="time", fill_value="extrapolate"
            )
    elif fill_nan == "mean":
        # fill with mean
        for var in da["variable"].values:
            var_data = da.sel(variable=var)  # select the data for the current variable
            mean_val = var_data.mean(
                dim="basin"
            )  # calculate the mean across all basins
            filled_data = var_data.fillna(
                mean_val
            )  # fill NaN values with the calculated mean
            da.loc[
                dict(variable=var)
            ] = filled_data  # update the original dataarray with the filled data
    elif fill_nan == "interpolate":
        # fill interpolation
        for i in range(da.shape[0]):
            da[i] = da[i].interpolate_na(dim="time", fill_value="extrapolate")
    else:
        raise NotImplementedError(f"fill_nan {fill_nan} not implemented")
    return da


class BaseDataset(Dataset):
    """Base data set class to load and preprocess data (batch-first) using PyTroch's Dataset"""

    def __init__(self, data_source: HydroDataset, data_params: dict, loader_type: str):
        """
        Parameters
        ----------
        data_source
            object for reading source data
        data_params
            parameters for reading source data
        loader_type
            train, vaild or test
        """
        super(BaseDataset, self).__init__()
        self.data_source = data_source
        self.data_params = data_params
        if loader_type in {"train", "valid", "test"}:
            self.loader_type = loader_type
        else:
            raise ValueError("'loader_type' must be one of 'train', 'valid' or 'test' ")
        # load and preprocess data
        self._load_data()

    def __len__(self):
        return self.num_samples

    def __getitem__(self, item: int):
        basin, time = self.lookup_table[item]
        seq_length = self.rho
        warmup_length = self.warmup_length
        x = (
            self.x.sel(
                basin=basin,
                time=slice(
                    time - np.timedelta64(warmup_length),
                    time + np.timedelta64(seq_length - 1, "D"),
                ),
            ).to_numpy()
        ).T
        if self.c is not None and self.c.shape[-1] > 0:
            c = self.c.sel(basin=basin).values
            c = np.tile(c, (seq_length, 1))
            x = np.concatenate((x, c), axis=1)
        y = (
            self.y.sel(
                basin=basin,
                time=slice(
                    time,
                    time + np.timedelta64(seq_length - 1, "D"),
                ),
            )
            .to_numpy()
            .T
        )
        return torch.from_numpy(x).float(), torch.from_numpy(y).float()

    def _load_data(self):
        train_mode = self.loader_type == "train"
        self.t_s_dict = wrap_t_s_dict(
            self.data_source, self.data_params, self.loader_type
        )
        data_flow_ds = self.data_source.read_ts_xrdataset(
            self.t_s_dict["sites_id"],
            self.t_s_dict["t_final_range"],
            self.data_params["target_cols"],
        )
        data_forcing_ds = self.data_source.read_ts_xrdataset(
            self.t_s_dict["sites_id"],
            self.t_s_dict["t_final_range"],
            self.data_params["relevant_cols"],
        )
        data_attr_ds = self.data_source.read_attr_xrdataset(
            self.t_s_dict["sites_id"],
            self.data_params["constant_cols"],
            all_number=True,
        )

        # trans to dataarray to better use xbatch
        if data_flow_ds is not None:
            data_flow_ds = unify_streamflow_unit(
                data_flow_ds, self.data_source.read_area(self.t_s_dict["sites_id"])
            )
            data_flow = self._trans2da_and_setunits(data_flow_ds)
        else:
            data_flow = None
        if data_forcing_ds is not None:
            data_forcing = self._trans2da_and_setunits(data_forcing_ds)
        else:
            data_forcing = None
        if data_attr_ds is not None:
            # firstly, we should transform some str type data to float type
            data_attr = self._trans2da_and_setunits(data_attr_ds)
        else:
            data_attr = None

        # normalization
        scaler_hub = ScalerHub(
            data_flow,
            data_forcing,
            data_attr,
            data_params=self.data_params,
            loader_type=self.loader_type,
            data_source=self.data_source,
        )

        self.x, self.y, self.c = self.kill_nan(scaler_hub.x, scaler_hub.c, scaler_hub.y)
        self.train_mode = train_mode
        self.rho = self.data_params["forecast_history"]
        self.target_scaler = scaler_hub.target_scaler
        self.warmup_length = self.data_params["warmup_length"]
        self._create_lookup_table()

    def _trans2da_and_setunits(self, ds):
        """Set units for dataarray transfromed from dataset"""
        result = ds.to_array(dim="variable")
        units_dict = {
            var: ds[var].attrs["units"]
            for var in ds.variables
            if "units" in ds[var].attrs
        }
        result.attrs["units"] = units_dict
        return result

    def kill_nan(self, x, c, y):
        data_params = self.data_params
        y_rm_nan = data_params["target_rm_nan"]
        x_rm_nan = data_params["relevant_rm_nan"]
        c_rm_nan = data_params["constant_rm_nan"]
        if x_rm_nan:
            # As input, we cannot have NaN values
            _fill_gaps_da(x, fill_nan="interpolate")
        if y_rm_nan:
            _fill_gaps_da(y, fill_nan="interpolate")
        if c_rm_nan:
            _fill_gaps_da(c, fill_nan="mean")
        return x, y, c

    def _create_lookup_table(self):
        lookup = []
        # list to collect basins ids of basins without a single training sample
        basins = self.t_s_dict["sites_id"]
        rho = self.rho
        warmup_length = self.warmup_length
        dates = self.y["time"].to_numpy()
        time_length = len(dates)
        for basin in tqdm(basins, file=sys.stdout, disable=False):
            # some dataloader load data with warmup period, so leave some periods for it
            # [warmup_len] -> time_start -> [rho]
            lookup.extend(
                (basin, dates[f])
                for f in range(warmup_length, time_length)
                if f < time_length - rho + 1
            )
        self.lookup_table = dict(enumerate(lookup))
        self.num_samples = len(self.lookup_table)


class BasinSingleFlowDataset(BaseDataset):
    """one time length output for each grid in a batch"""

    def __init__(self, data_source: HydroDataset, data_params: dict, loader_type: str):
        super(BasinSingleFlowDataset, self).__init__(
            data_source, data_params, loader_type
        )

    def __getitem__(self, index):
        xc, ys = super(BasinSingleFlowDataset, self).__getitem__(index)
        y = ys[-1, :]
        return xc, y

    def __len__(self):
        return self.num_samples


class BasinFlowDataset(BaseDataset):
    """Dataset for input of LSTM"""

    def __init__(self, data_source: HydroDataset, data_params: dict, loader_type: str):
        super(BasinFlowDataset, self).__init__(data_source, data_params, loader_type)

    def __getitem__(self, index):
        if self.train_mode:
            return super(BasinFlowDataset, self).__getitem__(index)
        # TODO: not CHECK warmup_length yet because we don't use warmup_length for pure DL models
        x = self.x[index, :, :]
        y = self.y[index, :, :]
        if self.c is None or self.c.shape[-1] == 0:
            return torch.from_numpy(x).float(), torch.from_numpy(y).float()
        c = self.c[index, :]
        c = np.repeat(c, x.shape[0], axis=0).reshape(c.shape[0], -1).T
        xc = np.concatenate((x, c), axis=1)
        return torch.from_numpy(xc).float(), torch.from_numpy(y).float()

    def __len__(self):
        return self.num_samples if self.train_mode else len(self.t_s_dict["sites_id"])


class KuaiDataset(BaseDataset):
    """mini-batch data model from Kuai Fang's paper: https://doi.org/10.1002/2017GL075619
    He used a random pick-up that we don't need to iterate all samples. Then, we can train model more quickly
    """

    def __init__(self, data_source: HydroDataset, data_params: dict, loader_type: str):
        """
        Parameters
        ----------
        data_source
            object for reading source data
        data_params
            parameters for reading source data
        loader_type
            train, vaild or test
        """
        super().__init__(data_source, data_params, loader_type)

    def __len__(self):
        if not self.train_mode:
            return len(self.t_s_dict["sites_id"])
        # batch_size * rho must be smaller than ngrid * nt, if not, the value logged will be negative that is wrong
        batch_size = self.data_params["batch_size"]
        rho = self.rho
        warmup_length = self.data_params["warmup_length"]
        ngrid = self.y.basin.shape[0]
        nt = self.y.time.shape[0]
        while batch_size * rho >= ngrid * nt:
            # try to use a smaller batch_size to make the model runnable
            batch_size = int(batch_size / 10)
        batch_size = max(batch_size, 1)
        n_iter_ep = int(
            np.ceil(
                np.log(0.01)
                / np.log(1 - batch_size * rho / ngrid / (nt - warmup_length))
            )
        )
        assert n_iter_ep >= 1
        # __len__ means the number of all samples, then, the number of loops in a epoch is __len__()/batch_size = n_iter_ep
        # hence we return n_iter_ep * batch_size
        return n_iter_ep * batch_size

    def __getitem__(self, index):
        if self.train_mode:
            return super(KuaiDataset, self).__getitem__(index)
        # TODO: not CHECK warmup_length yet because we don't use warmup_length for pure DL models
        basin = self.t_s_dict["sites_id"][index]
        x = self.x.sel(basin=basin).to_numpy().T
        y = self.y.sel(basin=basin).to_numpy().T
        if self.c is None or self.c.shape[-1] == 0:
            return torch.from_numpy(x).float(), torch.from_numpy(y).float()
        # TODO: not CHECK attributes reading
        c = self.c.sel(basin=basin).values
        c = np.repeat(c, x.shape[0], axis=0).reshape(c.shape[0], -1).T
        xc = np.concatenate((x, c), axis=1)
        return torch.from_numpy(xc).float(), torch.from_numpy(y).float()
