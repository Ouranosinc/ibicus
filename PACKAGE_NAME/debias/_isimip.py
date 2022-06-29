# (C) Copyright 1996- ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

from dataclasses import dataclass
from typing import Optional, Union

import attrs
import numpy as np
import scipy.special
import scipy.stats

from ..isimip_options import isimip_2_5, standard_variables_isimip
from ..utils import (
    StatisticalModel,
    day,
    ecdf,
    get_chunked_mean,
    get_yearly_mean,
    iecdf,
    interp_sorted_cdf_vals_on_given_length,
    month,
    sort_array_like_another_one,
    threshold_cdf_vals,
    year,
)
from ._debiaser import Debiaser


# Reference TODO
@attrs.define
class ISIMIP(Debiaser):
    # Variables
    distribution: Union[
        scipy.stats.rv_continuous, scipy.stats.rv_discrete, scipy.stats.rv_histogram, StatisticalModel
    ] = attrs.field(
        validator=attrs.validators.instance_of(
            (scipy.stats.rv_continuous, scipy.stats.rv_discrete, scipy.stats.rv_histogram, StatisticalModel)
        )
    )
    trend_preservation_method: str = attrs.field(
        validator=attrs.validators.in_(["additive", "multiplicative", "mixed", "bounded"])
    )
    detrending: bool = attrs.field(validator=attrs.validators.instance_of(bool))
    reasonable_physical_range: Optional[list] = attrs.field(default=None)

    @reasonable_physical_range.validator
    def validate_reasonable_physical_range(self, attribute, value):
        if value is not None:
            if len(value) != 2:
                raise ValueError("reasonable_physical_range should have only a lower and upper physical range")
            if not all(isinstance(elem, (int, float)) for elem in value):
                raise ValueError("reasonable_physical_range needs to be a list of floats")
            if not value[0] < value[1]:
                raise ValueError("lower bounds needs to be smaller than upper bound in reasonable_physical_range")

    variable: str = attrs.field(default="unknown", eq=False)

    # Bounds
    lower_bound: float = attrs.field(default=np.inf, validator=attrs.validators.instance_of(float), converter=float)
    lower_threshold: float = attrs.field(default=np.inf, validator=attrs.validators.instance_of(float), converter=float)
    upper_bound: float = attrs.field(default=-np.inf, validator=attrs.validators.instance_of(float), converter=float)
    upper_threshold: float = attrs.field(
        default=-np.inf, validator=attrs.validators.instance_of(float), converter=float
    )

    # ISIMIP behavior
    trend_removal_with_significance_test: bool = attrs.field(
        default=True, validator=attrs.validators.instance_of(bool)
    )  # step 3
    trend_transfer_only_for_values_within_threshold: bool = attrs.field(
        default=True, validator=attrs.validators.instance_of(bool)
    )  # step 5
    event_likelihood_adjustment: bool = attrs.field(
        default=False, validator=attrs.validators.instance_of(bool)
    )  # step 6

    # math functions
    ecdf_method: str = attrs.field(
        default="inverted_cdf",
        validator=attrs.validators.in_(["kernel_density", "linear_interpolation", "step_function"]),
    )
    iecdf_method: str = attrs.field(
        default="step_function",
        validator=attrs.validators.in_(
            [
                "inverted_cdf",
                "averaged_inverted_cdf",
                "closest_observation",
                "interpolated_inverted_cdf",
                "hazen",
                "weibull",
                "linear",
                "median_unbiased",
                "normal_unbiased",
            ]
        ),
    )

    # iteration
    running_window_mode: bool = attrs.field(default=True, validator=attrs.validators.instance_of(bool))
    running_window_length: int = attrs.field(
        default=31,
        validator=[attrs.validators.instance_of(int), attrs.validators.gt(0)],
    )
    running_window_step_length: int = attrs.field(
        default=1, validator=[attrs.validators.instance_of(int), attrs.validators.gt(0)]
    )

    @classmethod
    def from_variable(cls, variable):
        if variable not in standard_variables_isimip.keys():
            raise ValueError("variable needs to be one of %s" % standard_variables_isimip.keys())
        isimip_instance = cls(
            variable=variable,
            **isimip_2_5.get("variables").get(variable),
            **isimip_2_5.get("isimip_run"),
        )
        return isimip_instance

    @property
    def has_lower_threshold(self):
        if self.lower_threshold is not None and self.lower_threshold > -np.inf:
            return True
        else:
            return False

    @property
    def has_lower_bound(self):
        if self.lower_bound is not None and self.lower_bound > -np.inf:
            return True
        else:
            return False

    @property
    def has_upper_threshold(self):
        if self.upper_threshold is not None and self.upper_threshold < np.inf:
            return True
        else:
            return False

    @property
    def has_upper_bound(self):
        if self.upper_bound is not None and self.upper_bound < np.inf:
            return True
        else:
            return False

    # ----- Non public helpers: General ----- #

    def _get_mask_for_values_beyond_lower_threshold(self, x):
        return x <= self.lower_threshold

    def _get_mask_for_values_beyond_upper_threshold(self, x):
        return x >= self.upper_threshold

    def _get_mask_for_values_between_thresholds(self, x):
        return (x > self.lower_threshold) & (x < self.upper_threshold)

    def _get_values_between_thresholds(self, x):
        return x[self._get_mask_for_values_between_thresholds(x)]

    def _check_reasonable_physical_range(self, obs_hist, cm_hist, cm_future):
        if self.reasonable_physical_range is not None:
            if np.any((obs_hist < self.reasonable_physical_range[0]) | (obs_hist > self.reasonable_physical_range[1])):
                raise ValueError(
                    "Values of obs_hist lie outside the reasonable physical range of %s"
                    % self.reasonable_physical_range
                )

            if np.any((cm_hist < self.reasonable_physical_range[0]) | (cm_hist > self.reasonable_physical_range[1])):
                raise ValueError(
                    "Values of cm_hist lie outside the reasonable physical range of %s" % self.reasonable_physical_range
                )

            if np.any(
                (cm_future < self.reasonable_physical_range[0]) | (cm_future > self.reasonable_physical_range[1])
            ):
                raise ValueError(
                    "Values of cm_future lie outside the reasonable physical range of %s"
                    % self.reasonable_physical_range
                )

    # ----- Non public helpers: ISIMIP-steps ----- #

    def _step3_remove_trend(self, x, years=None):
        if years is None:
            annual_means = get_chunked_mean(x, self.running_window_length)
        else:
            annual_means = get_yearly_mean(x, years)

        years = np.arange(annual_means.size)
        regression = scipy.stats.linregress(years, annual_means)
        if regression.pvalue < 0.05 and self.trend_removal_with_significance_test:
            annual_trend = regression.slope * (years - np.mean(years))
        else:
            annual_trend = np.zeros(years.size, dtype=x.dtype)
        trend = np.repeat(annual_trend, self.running_window_length)[0 : x.size]
        x = x - trend
        return x, trend

    def _step5_transfer_trend(self, obs_hist, cm_hist, cm_future):
        # Compute p = F_obs_hist(x) with x in obs_hist
        p = ecdf(obs_hist, obs_hist, method=self.ecdf_method)

        # Compute q-vals: q = IECDF(p)
        q_obs_hist = obs_hist  # TODO: = iecdf_obs_hist(p), appears in eq. 7
        q_cm_future = iecdf(cm_future, p, method=self.iecdf_method)
        q_cm_hist = iecdf(cm_hist, p, method=self.iecdf_method)

        if self.trend_preservation_method == "additive":
            delta_add = q_cm_future - q_cm_hist
            return obs_hist + delta_add
        elif self.trend_preservation_method == "multiplicative":
            delta_star_mult = np.where(q_cm_hist == 0, 1, q_cm_future / q_cm_hist)
            delta_mult = np.maximum(0.01, np.minimum(100, delta_star_mult))
            return obs_hist * delta_mult
        elif self.trend_preservation_method == "mixed":
            # Formula 7
            condition1 = q_cm_hist >= q_obs_hist
            condition2 = (q_cm_hist < q_obs_hist) & (q_obs_hist < 9 * q_cm_hist)

            gamma = np.zeros(len(obs_hist))
            gamma[condition1] = 1
            gamma[condition2] = 0.5 * (1 + np.cos(q_obs_hist[condition2] / q_cm_hist[condition2] - 1) * np.pi / 8)

            # Formula 6
            delta_add = q_cm_future - q_cm_hist
            delta_star_mult = np.where(q_cm_hist == 0, 1, q_cm_future / q_cm_hist)
            delta_mult = np.maximum(0.01, np.minimum(100, delta_star_mult))
            return gamma * obs_hist * delta_mult + (1 - gamma) * (obs_hist + delta_add)
        elif self.trend_preservation_method == "bounded":
            a = self.lower_bound
            b = self.upper_bound

            # Formula 8
            condition1 = q_cm_hist > q_cm_future
            condition2 = np.isclose(q_cm_hist, q_cm_future)

            return_vals = b - (b - obs_hist) * (b - q_cm_future) / (b - q_cm_hist)
            return_vals[condition1] = a + (obs_hist[condition1] - a) * (q_cm_future[condition1] - a) / (
                q_cm_hist[condition1] - a
            )
            return_vals[condition2] = obs_hist[condition2]
            return return_vals
        else:
            raise ValueError(
                """ Wrong value for self.trend_preservation_method.
                    Needs to be one of ['additive', 'multiplicative', 'mixed', 'bounded'] """
            )

    def _step6_calculate_percent_values_beyond_threshold(
        mask_for_values_beyond_threshold,
    ):
        return mask_for_values_beyond_threshold.sum() / mask_for_values_beyond_threshold.size

    @staticmethod
    def _step6_get_P_obs_future(P_obs_hist, P_cm_hist, P_cm_future):
        if np.isclose(P_cm_hist, P_obs_hist):
            return P_cm_future
        elif P_cm_future <= P_cm_hist and P_cm_hist > P_obs_hist:
            return P_obs_hist * P_cm_future / P_cm_hist
        elif P_cm_future >= P_cm_hist and P_cm_hist < P_obs_hist:
            return 1 - (1 - P_obs_hist) * (1 - P_cm_future) / (1 - P_cm_hist)
        else:
            return P_obs_hist + P_cm_future - P_cm_hist

    def _step6_get_nr_of_entries_to_set_to_bound(
        self,
        mask_for_values_beyond_threshold_obs_hist_sorted,
        mask_for_values_beyond_threshold_cm_hist_sorted,
        mask_for_values_beyond_threshold_cm_future_sorted,
    ):

        P_obs_hist = ISIMIP._step6_calculate_percent_values_beyond_threshold(
            mask_for_values_beyond_threshold_obs_hist_sorted
        )
        P_cm_hist = ISIMIP._step6_calculate_percent_values_beyond_threshold(
            mask_for_values_beyond_threshold_cm_hist_sorted
        )
        P_cm_future = ISIMIP._step6_calculate_percent_values_beyond_threshold(
            mask_for_values_beyond_threshold_cm_future_sorted
        )

        P_obs_future = ISIMIP._step6_get_P_obs_future(P_obs_hist, P_cm_hist, P_cm_future)

        return round(mask_for_values_beyond_threshold_cm_future_sorted.size * P_obs_future)

    @staticmethod
    def _step6_transform_nr_of_entries_to_set_to_lower_bound_to_mask_for_cm_future(nr, cm_future_sorted):
        mask = np.zeros_like(cm_future_sorted, dtype=bool)
        mask[0:nr] = True
        return mask

    def _step6_get_mask_for_entries_to_set_to_lower_bound(self, obs_hist_sorted, cm_hist_sorted, cm_future_sorted):

        nr_of_entries_to_set_to_lower_bound = self._step6_get_nr_of_entries_to_set_to_bound(
            self._get_mask_for_values_beyond_lower_threshold(obs_hist_sorted),
            self._get_mask_for_values_beyond_lower_threshold(cm_hist_sorted),
            self._get_mask_for_values_beyond_lower_threshold(cm_future_sorted),
        )

        mask_for_entries_to_set_to_lower_bound = (
            ISIMIP._step6_transform_nr_of_entries_to_set_to_lower_bound_to_mask_for_cm_future(
                nr_of_entries_to_set_to_lower_bound, cm_future_sorted
            )
        )
        return mask_for_entries_to_set_to_lower_bound

    @staticmethod
    def _step6_transform_nr_of_entries_to_set_to_upper_bound_to_mask_for_cm_future(nr, cm_future_sorted):
        mask = np.zeros_like(cm_future_sorted, dtype=bool)
        mask[(cm_future_sorted.size - nr) :] = True
        return mask

    def _step6_get_mask_for_entries_to_set_to_upper_bound(self, obs_hist_sorted, cm_hist_sorted, cm_future_sorted):

        nr_of_entries_to_set_to_upper_bound = self._step6_get_nr_of_entries_to_set_to_bound(
            self._get_mask_for_values_beyond_upper_threshold(obs_hist_sorted),
            self._get_mask_for_values_beyond_upper_threshold(cm_hist_sorted),
            self._get_mask_for_values_beyond_upper_threshold(cm_future_sorted),
        )

        mask_for_entries_to_set_to_upper_bound = (
            ISIMIP._step6_transform_nr_of_entries_to_set_to_upper_bound_to_mask_for_cm_future(
                nr_of_entries_to_set_to_upper_bound, cm_future_sorted
            )
        )
        return mask_for_entries_to_set_to_upper_bound

    def _step6_adjust_values_between_thresholds(
        self, obs_hist_sorted, obs_future_sorted, cm_hist_sorted, cm_future_sorted
    ):

        # Calculate cdf-fits
        fit_cm_future = self.distribution.fit(cm_future_sorted)

        fit_obs_hist = self.distribution.fit(obs_hist_sorted)
        fit_obs_future = self.distribution.fit(obs_future_sorted)
        fit_cm_hist = self.distribution.fit(cm_hist_sorted)

        # Get the cdf-vals and interpolate if there are unequal sample sizes (following Switanek 2017):
        cdf_vals_cm_future = threshold_cdf_vals(self.distribution.cdf(cm_future_sorted, *fit_cm_future))

        cdf_vals_obs_hist = interp_sorted_cdf_vals_on_given_length(
            threshold_cdf_vals(self.distribution.cdf(obs_hist_sorted, *fit_obs_hist)),
            cdf_vals_cm_future.size,
        )
        cdf_vals_cm_hist = interp_sorted_cdf_vals_on_given_length(
            threshold_cdf_vals(self.distribution.cdf(cm_hist_sorted, *fit_cm_hist)),
            cdf_vals_cm_future.size,
        )

        # Event likelihood adjustment only happens in certain cases
        if not self.event_likelihood_adjustment:
            mapped_vals = self.distribution.ppf(cdf_vals_cm_future, *fit_obs_future)
        else:
            # Calculate L-values and delta log-odds for mapping, following formula 11-14
            L_obs_hist = scipy.special.logit(cdf_vals_obs_hist)
            L_cm_hist = scipy.special.logit(cdf_vals_cm_hist)
            L_cm_future = scipy.special.logit(cdf_vals_cm_future)

            delta_log_odds = np.maximum(-np.log(10), np.minimum(np.log(10), L_cm_future - L_cm_hist))

            # Map values following formula 10
            mapped_vals = self.distribution.ppf(scipy.special.expit(L_obs_hist + delta_log_odds), *fit_obs_future)
        return mapped_vals

    # ----- ISIMIP calculation steps ----- #
    def step1(self, obs_hist, cm_hist, cm_future):
        scale = None
        if self.variable == "rsds":
            # TODO
            pass

        return obs_hist, cm_hist, cm_future, scale

    def step2(self, obs_hist, cm_hist, cm_future):
        """
        Step 2: impute values for prsnratio which are missing on days where there is no precipitation. They are imputed by effectively sampling the iecdf (see Lange 2019 and ISIMIP3b factsheet for the method).
        """
        if self.variable == "prsnratio":
            obs_hist = np.where(
                np.isnan(obs_hist),
                iecdf(x=obs_hist, p=np.random.uniform(size=obs_hist.size)),
                obs_hist,
            )
            cm_hist = np.where(
                np.isnan(cm_hist),
                iecdf(x=cm_hist, p=np.random.uniform(size=cm_hist.size)),
                cm_hist,
            )
            cm_future = np.where(
                np.isnan(cm_future),
                iecdf(x=cm_future, p=np.random.uniform(size=cm_future.size)),
                cm_future,
            )

        return obs_hist, cm_hist, cm_future

    def step3(self, obs_hist, cm_hist, cm_future, years_obs_hist=None, years_cm_hist=None, years_cm_future=None):
        """
        Step 3: Linear trend removal if detrending = True. This is because certain variables (eg. temp) can have substantial trends also within training and application period (not only between).
        These trends are removed to "prevent a confusion of these trends with interannual variability during quantile mapping (steps 5 and6)" (Lange 2019). The trend for cm_future is subsequently added again in step7.
        Trends are calculated by linearly regressing the yearly mean values y_i against years t_i. From each observation in year t_i then the slope * (t_i - mean(t_i)) is removed (normalising the trend such that the sum over all t_i is zero). See Lange 2019 for the exact method.

        If trend_removal_with_significance_test = True (default in ISIMIP v2.5) then linear trends are only removed subject it being significant (p-value < 0.05).
        If years_obs_hist, years_cm_hist or/and years_cm_future are given the yearly means can be calculated exactly because it is known to which year which observation pertains.
        If they are None then it is assumed that observations are always group in chunks of self.window_size and the observation afterwards is part of a new year. This can lead to small inconsistencies with leap years, however the problems should be minor.
        """
        trend_cm_future = np.zeros_like(cm_future)
        if self.detrending:
            obs_hist, _ = self._step3_remove_trend(obs_hist, years_obs_hist)
            cm_hist, _ = self._step3_remove_trend(cm_hist, years_cm_hist)
            cm_future, trend_cm_future = self._step3_remove_trend(cm_future, years_cm_future)
        return obs_hist, cm_hist, cm_future, trend_cm_future

    def step4(self, cm_future):
        """
        Step4: If the variable is bounded then values between the threshold and corresponding bound (including values equal to the bound) are
        randomized uniformly between the threshold and bound and resorted according to the order in which they were before.
        """
        if self.has_lower_bound and self.has_lower_threshold:
            mask_cm_future_values_beyond_lower_threshold = self._get_mask_for_values_beyond_lower_threshold(cm_future)
            randomised_values_between_threshold_and_bound = np.sort(
                np.random.uniform(
                    size=mask_cm_future_values_beyond_lower_threshold.sum(),
                    low=self.lower_bound,
                    high=self.lower_threshold,
                )
            )
            cm_future[mask_cm_future_values_beyond_lower_threshold] = sort_array_like_another_one(
                randomised_values_between_threshold_and_bound, cm_future[mask_cm_future_values_beyond_lower_threshold]
            )

        if self.has_upper_bound and self.has_upper_threshold:
            mask_cm_future_values_beyond_upper_threshold = self._get_mask_for_values_beyond_upper_threshold(cm_future)
            randomised_values_between_threshold_and_bound = np.sort(
                np.random.uniform(
                    size=mask_cm_future_values_beyond_upper_threshold.sum(),
                    low=self.upper_threshold,
                    high=self.upper_bound,
                )
            )
            cm_future[mask_cm_future_values_beyond_lower_threshold] = sort_array_like_another_one(
                randomised_values_between_threshold_and_bound, cm_future[mask_cm_future_values_beyond_upper_threshold]
            )

        return cm_future

    # Generate pseudo future observations and transfer trends
    def step5(self, obs_hist, cm_hist, cm_future):
        """
        Step 5: generates pseudo future observations by transfering simulated trends to historical recorded observations.
        This makes the ISIMIP-method trend-preserving.
        """
        if self.trend_transfer_only_for_values_within_threshold:
            return self._step5_transfer_trend(obs_hist, cm_hist, cm_future)
        else:
            mask_for_values_between_thresholds_obs_hist = self._get_mask_for_values_between_thresholds(obs_hist)
            obs_future = obs_hist
            obs_future[mask_for_values_between_thresholds_obs_hist] = self._step5_transfer_trend(
                obs_hist[mask_for_values_between_thresholds_obs_hist],
                self._get_values_between_thresholds(cm_hist),
                self._get_values_between_thresholds(cm_future),
            )

    # Core of the isimip-method: parametric quantile mapping
    def step6(self, obs_hist, obs_future, cm_hist, cm_future):
        """
        Step 6: parametric quantile mapping between cm_future and obs_future (the pseudo-future observations) to debias the former. Core of the bias adjustment method.
        For (partly) bounded climate variables additionally the frequency of values is bias-adjusted before the other observations are quantile mapped.
        If event_likelihood_adjustment = True then additionally to "normal quantile mapping" the likelihood of individual events is adjusted (see Lange 2019 for the method which is based on Switanek 2017).
        """

        # Sort arrays to apply parametric quantile mapping (values of equal rank are mapped together).
        # Save sort-order of cm_future for backsorting
        cm_future_argsort = np.argsort(cm_future)
        cm_future_sorted = cm_future[cm_future_argsort]

        obs_hist_sorted = np.sort(obs_hist)
        obs_future_sorted = np.sort(obs_future)
        cm_hist_sorted = np.sort(cm_hist)

        # Calculate values that are set to lower bound for bounded/thresholded variables
        mask_for_entries_to_set_to_lower_bound = np.zeros_like(cm_future_sorted, dtype=bool)
        if self.has_lower_threshold:
            mask_for_entries_to_set_to_lower_bound = self._step6_get_mask_for_entries_to_set_to_lower_bound(
                obs_hist_sorted, cm_hist_sorted, cm_future_sorted
            )

        # Calculate values that are set to lower bound for bounded/thresholded variables
        mask_for_entries_to_set_to_upper_bound = np.zeros_like(cm_future_sorted, dtype=bool)
        if self.has_upper_threshold:
            mask_for_entries_to_set_to_upper_bound = self._step6_get_mask_for_entries_to_set_to_upper_bound(
                obs_hist_sorted, cm_hist_sorted, cm_future_sorted
            )

        # Set values to upper or lower bound for bounded/thresholded variables
        cm_future_sorted[mask_for_entries_to_set_to_lower_bound] = self.lower_bound
        cm_future_sorted[mask_for_entries_to_set_to_upper_bound] = self.upper_bound
        mask_for_entries_not_set_to_either_bound = np.logical_and(
            np.logical_not(mask_for_entries_to_set_to_lower_bound),
            np.logical_not(mask_for_entries_to_set_to_upper_bound),
        )

        # Calculate values between bounds
        cm_future_sorted[mask_for_entries_not_set_to_either_bound] = self._step6_adjust_values_between_thresholds(
            self._get_values_between_thresholds(obs_hist_sorted),
            self._get_values_between_thresholds(obs_future_sorted),
            self._get_values_between_thresholds(cm_hist_sorted),
            cm_future_sorted[mask_for_entries_not_set_to_either_bound],
        )

        # Return values inserted back at correct locations
        reverse_sorting_idx = np.argsort(cm_future_argsort)
        return cm_future_sorted[reverse_sorting_idx]

    def step7(self, cm_future, trend_cm_future):
        """
        Step 7: If detrending = True add the trend removed in step 3 back to the debiased cm_future values.
        """
        if self.detrending:
            cm_future = cm_future + trend_cm_future
        return cm_future

    def step8(self, cm_future, scale):
        if self.variable == "rsds":
            pass
        return cm_future

    # ----- Apply location function -----
    def _apply_on_window(
        self,
        obs_hist: np.ndarray,
        cm_hist: np.ndarray,
        cm_future: np.ndarray,
        years_obs_hist: Optional[np.ndarray] = None,
        years_cm_hist: Optional[np.ndarray] = None,
        years_cm_future: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        # Steps
        obs_hist, cm_hist, cm_future, scale = self.step1(obs_hist, cm_hist, cm_future)
        obs_hist, cm_hist, cm_future = self.step2(obs_hist, cm_hist, cm_future)
        obs_hist, cm_hist, cm_future, trend_cm_future = self.step3(
            obs_hist, cm_hist, cm_future, years_obs_hist, years_cm_hist, years_cm_future
        )
        cm_future = self.step4(cm_future)
        obs_future = self.step5(obs_hist, cm_hist, cm_future)
        cm_future = self.step6(obs_hist, obs_future, cm_hist, cm_future)
        cm_future = self.step7(cm_future, trend_cm_future)
        cm_future = self.step8(cm_future, scale)

        return cm_future

    def apply_location(
        self,
        obs_hist: np.ndarray,
        cm_hist: np.ndarray,
        cm_future: np.ndarray,
        time_obs_hist: Optional[np.ndarray] = None,
        time_cm_hist: Optional[np.ndarray] = None,
        time_cm_future: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        self._check_reasonable_physical_range(obs_hist, cm_hist, cm_future)

        # TODO: how do we integrate if day of year or month is passed as an argument
        if self.running_window_mode:
            return cm_future
        else:
            if time_obs_hist is not None and time_cm_hist is not None and time_cm_future is not None:

                debiased_cm_future = np.zeros_like(cm_future)
                for i_month in range(1, 13):
                    mask_i_month_in_obs_hist = month(time_obs_hist) == i_month
                    mask_i_month_in_cm_hist = month(time_cm_hist) == i_month
                    mask_i_month_in_cm_future = month(time_cm_future) == i_month

                    debiased_cm_future[mask_i_month_in_cm_future] = self._apply_on_window(
                        obs_hist=obs_hist[mask_i_month_in_obs_hist],
                        cm_hist=cm_hist[mask_i_month_in_cm_hist],
                        cm_future=cm_future[mask_i_month_in_cm_future],
                        years_obs_hist=year(time_obs_hist[mask_i_month_in_obs_hist]),
                        years_cm_hist=year(time_cm_hist[mask_i_month_in_cm_hist]),
                        years_cm_future=year(time_cm_future[mask_i_month_in_cm_future]),
                    )
                return debiased_cm_future
            else:
                raise ValueError(
                    "ISIMIP not in running window mode requires time information. "
                    "Please specify time_obs_hist, time_cm_hist, time_cm_future or change to running window mode."
                )

    @staticmethod
    def _unpack_kwargs_and_get_locationwise_info(i, j, **kwargs):
        return {
            key: value[
                :,
                i,
                j,
            ]
            for key, value in kwargs.items()
        }

    def apply(self, obs, cm_hist, cm_future, **kwargs):
        print("----- Running debiasing -----")
        Debiaser.check_inputs(obs, cm_hist, cm_future)
        output = np.empty([cm_future.shape[0], obs.shape[1], obs.shape[2]])
        for i, j in tqdm(np.ndindex(obs.shape[1:]), total=np.prod(obs.shape[1:])):
            output[:, i, j] = self.apply_location(
                obs[:, i, j],
                cm_hist[:, i, j],
                cm_future[:, i, j],
                **ISIMIP._unpack_kwargs_and_get_locationwise_info(i, j, **kwargs),
            )

        return output
