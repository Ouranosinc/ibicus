# (C) Copyright 1996- ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

from typing import Union

import attrs
import scipy.stats

from ..utils import PrecipitationHurdleModelGamma, StatisticalModel
from ..variables import Variable, map_standard_precipitation_method, pr, tas
from ._debiaser import Debiaser

default_settings = {
    tas: {"distribution": scipy.stats.beta},
    pr: {"distribution": PrecipitationHurdleModelGamma},
}


@attrs.define(slots=False)
class ECDFM(Debiaser):
    """
    |br| Implements Equidistant CDF Matching (ECDFM) following Li et al. 2010.

    Let cm refer to climate model output, obs to observations and hist/future to whether the data was collected from the reference period or is part of future projections.
    Let :math:`F_{\\text{cm_hist}}` be the cdf fitted as a parametric distribution to climate model output data in the reference period. The future climate projections :math:`x_{\\text{cm_fut}}` are then mapped to:

    .. math:: x_{\\text{cm_fut}} \\rightarrow x_{\\text{cm_fut}} - F^{-1}_{\\text{cm_hist}}(F_{\\text{cm_fut}}(x_{\\text{cm_fut}})) + F^{-1}_{\\text{obs}}(F_{\\text{cm_fut}}(x_{\\text{cm_fut}})) 

    The difference between the future climate model data and the future climate model data quantile mapped to historical climate model data (de facto future model data bias corrected to historical model data) is added to a quantile mapping bias correction between observations and future climate model data.
    In essence, this method says that future climate model data can be bias corrected directly with reference period observations, if the quantile specific difference between present-day and future climate model simulations is taken into account.
    This allows for changes in higher moments in the climate model, compared to standard Quantile Mapping where just the mean is assumed to change in future climate. 

    The method was originally developed with monthly data in view, however the authors of this package think that there is no reason for the method not to be applicable to daily data.

    .. note:: As opposed to most other publications, Li et al. use a  4-parameter beta distribution (:py:data:`scipy.stats.beta`) for ``tas`` instead of a normal distribution. This can be slow for the fit at times. Consider modifying the ``distribution`` parameter for ``tas``.

    For precipitation a distribution or model is needed that accounts for mixed zero and positive value character. Default is a precipitation hurdle model (TODO: reference). However also different ones are possible. :py:func:`for_precipitation` helps with the initialisation of different precipitation methods.


    **Reference**:

    - Li, H., Sheffield, J., and Wood, E. F. (2010), Bias correction of monthly precipitation and temperature fields from Intergovernmental Panel on Climate Change AR4 models using equidistant quantile matching, J. Geophys. Res., 115, D10101, doi:10.1029/2009JD012882.

    |br|

    Attributes
    ----------
    distribution : Union[scipy.stats.rv_continuous, scipy.stats.rv_discrete, scipy.stats.rv_histogram, StatisticalModel]
        Method used for the fit to the historical and future climate model outputs as well as the observations.
        Usually a distribution in ``scipy.stats.rv_continuous``, but can also be an empirical distribution as given by ``scipy.stats.rv_histogram`` or a more complex statistical model as wrapped by the ``StatisticalModel``(TODO: reference) class.
    variable : str
        Variable for which the debiasing is done. Default: "unknown".
    """

    distribution: Union[
        scipy.stats.rv_continuous, scipy.stats.rv_discrete, scipy.stats.rv_histogram, StatisticalModel
    ] = attrs.field(
        validator=attrs.validators.instance_of(
            (scipy.stats.rv_continuous, scipy.stats.rv_discrete, scipy.stats.rv_histogram, StatisticalModel)
        )
    )

    @classmethod
    def from_variable(cls, variable: Union[str, Variable], **kwargs):
        return super()._from_variable(cls, variable, default_settings, **kwargs)

    @classmethod
    def for_precipitation(
        cls,
        model_type: str = "hurdle",
        amounts_distribution: scipy.stats.rv_continuous = scipy.stats.gamma,
        censoring_threshold: float = 0.1,
        hurdle_model_randomization: bool = True,
        hurdle_model_kwds_for_distribution_fit={"floc": 0, "fscale": None},
        **kwargs
    ):
        """
        Instanciates the class to a precipitation-debiaser. This allows granular setting of available precipitation models without needing to explicitly specify the precipitation censored model for example.

        Parameters
        ----------
        model_type : str
            One of ``["censored", "hurdle", "ignore_zeros"]``. Model type to be used. See utils.gen_PrecipitationGammaLeftCensoredModel, utils.gen_PrecipitationHurdleModel and utils.gen_PrecipitationIgnoreZeroValuesModel for more details. TODO: reference
        amounts_distribution : scipy.stats.rv_continuous
            Distribution used for precipitation amounts. For the censored model only :py:data:`scipy.stats.gamma` is possible.
        censoring_threshold : float
            The censoring-value if a censored precipitation model is used.
        hurdle_model_randomization : bool
            Whether when computing the cdf-values for a hurdle model randomization shall be used. See utils.gen_PrecipitationHurdleModel for more details. TODO: reference
        hurdle_model_kwds_for_distribution_fit : dict
            Dict of parameters used for the distribution fit inside a hurdle model. Default: ``{"floc": 0, "fscale": None} location of distribution is fixed at zero (``floc = 0``) to stabilise Gamma distribution fits in scipy.
        **kwargs:
            All other class attributes that shall be set and where the standard values shall be overwritten.

        """
        variable = pr

        method = map_standard_precipitation_method(
            model_type,
            amounts_distribution,
            censoring_threshold,
            hurdle_model_randomization,
            hurdle_model_kwds_for_distribution_fit,
        )
        parameters = {"distribution": method, "variable": variable.name}
        return cls(**{**parameters, **kwargs})

    def apply_location(self, obs, cm_hist, cm_future):
        fit_obs = self.distribution.fit(obs)
        fit_cm_hist = self.distribution.fit(cm_hist)
        fit_cm_future = self.distribution.fit(cm_future)

        return (
            cm_future
            + self.distribution.ppf(self.distribution.cdf(cm_future, *fit_cm_future), *fit_obs)
            - self.distribution.ppf(self.distribution.cdf(cm_future, *fit_cm_future), *fit_cm_hist)
        )
