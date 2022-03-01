from typing import List, Tuple

import numpy as np

import pandas as pd


# These numbers come from "simple_heat_pump_model.ipynb" in data-exploration project
ELEC_INTERCEPT_COEF = -5.195751e-01
ELEC_RPM_SQUARED_COEF = 1.375397e-07
ELEC_FORWARD_TEMP_COEF = 3.693311e-02
ELEC_FORWARD_TEMP_TIMES_RPM_COEF = 2.581335e-05
HEAT_INTERCEPT_COEF = 0.520527
HEAT_RPM_COEF = 0.007857
HEAT_FORWARD_TEMP_TIMES_RPM_COEF = -0.000017
HEAT_BRINE_TEMP_TIMES_RPM_COEF = 0.000188


DEFAULT_COP = 4.6  # Specified in technical description of "Thermium Mega" heat pump
DEFAULT_BRINE_TEMP = 0
DEFAULT_FORWARD_TEMP = 55
RPM_MIN = 1500
RPM_MAX = 6000

MIN_WORKLOAD = 1
MAX_WORKLOAD = 10
POSSIBLE_WORKLOADS_WHEN_RUNNING: List[int] = np.arange(MIN_WORKLOAD, MAX_WORKLOAD + 1).tolist()


class HeatPump:
    """
    A component to allow building agents to convert electricity to heat.
    """
    coeff_of_perf: float

    def __init__(self, coeff_of_perf=DEFAULT_COP):
        # Default value taken from technical description of "Thermia Mega", size Medium.
        # COP=4.6 is achieved with brine temp 0, forward temp 35, RPM 3600.
        self.coeff_of_perf = coeff_of_perf

    def calculate_energy(self, workload: int, forward_temp_c: float = DEFAULT_FORWARD_TEMP,
                         brine_temp_c: float = DEFAULT_BRINE_TEMP) -> Tuple[float, float]:
        """
        Use simple linear models to calculate the electricity needed, and amount of heat produced, for a medium sized
        "Thermia" heat pump. See "simple_heat_pump_model.ipynb" in data-exploration project.
        Heat produced is then scaled using self.coeff_of_perf.

        @param workload: An integer describing the workload, or gear, of the heat pump. This being 0 corresponds to
            the heat pump not running at all, requiring no electricity and producing no heat.
        @param forward_temp_c: The setpoint in degrees Celsius. Models were fit using only 2 unique values; 35 and 55,
            so this preferably shouldn't deviate too far from those.
        @param brine_temp_c: The temperature of the brine fluid in degrees Celsius. Models were fit using only 3 unique
            values; -5, 0 and 5, so this preferably shouldn't deviate too far from those.

        @return A Tuple: First value being the amount of electricity needed to run the heat pump with the given
            settings, and the second value being the expected amount of heat produced by those settings. Units for both
            is kilowatt.
        """
        if workload == 0:
            return 0, 0

        # Convert workload to rpm
        rpm = map_workload_to_rpm(workload)

        # Calculate electricity needed, and heat output, for this setting
        predicted_elec = model_elec_needed(forward_temp_c, rpm)
        predicted_heat_normal_thermia = model_heat_output(forward_temp_c, rpm, brine_temp_c)

        predicted_heat = predicted_heat_normal_thermia * self.coeff_of_perf / DEFAULT_COP

        return predicted_elec, predicted_heat

    def calculate_for_all_workloads(self, forward_temp_c: float = DEFAULT_FORWARD_TEMP,
                                    brine_temp_c: float = DEFAULT_BRINE_TEMP) -> pd.DataFrame:
        """
        Returns workload, electricity needed, heating produced
        """
        # Want to evaluate all possible gears, and also to not run the heat pump at all
        workloads: List[int] = [0] + POSSIBLE_WORKLOADS_WHEN_RUNNING
        elec_input = []
        heat_output = []
        for workload in workloads:
            predicted_elec, predicted_heat = self.calculate_energy(workload, forward_temp_c, brine_temp_c)
            elec_input.append(predicted_elec)
            heat_output.append(predicted_heat)

        frame = pd.DataFrame({'workload': workloads, 'input': elec_input, 'output': heat_output})
        return frame


class ValueOutOfRangeError(Exception):
    """
    Raised when an input is out of valid range.
    """
    pass


def model_elec_needed(forward_temp_c: float, rpm: float) -> float:
    return ELEC_INTERCEPT_COEF + \
        ELEC_RPM_SQUARED_COEF * rpm * rpm + \
        ELEC_FORWARD_TEMP_COEF * forward_temp_c + \
        ELEC_FORWARD_TEMP_TIMES_RPM_COEF * forward_temp_c * rpm


def model_heat_output(forward_temp_c: float, rpm: float, brine_temp_c: float) -> float:
    return HEAT_INTERCEPT_COEF + \
        HEAT_RPM_COEF * rpm + \
        HEAT_FORWARD_TEMP_TIMES_RPM_COEF * forward_temp_c * rpm + \
        HEAT_BRINE_TEMP_TIMES_RPM_COEF * brine_temp_c * rpm


def map_workload_to_rpm(workload: float, rpm_min: float = RPM_MIN, rpm_max: float = RPM_MAX) -> float:
    """
    Function to perform a linear mapping of an input workload into an output rpm.
    The workload refers to the "intensity-step"/gear - setting on which the heat-pump shall work. As such, it is
    expected to be in the range 1 - 10.
    """
    if workload < MIN_WORKLOAD or workload > MAX_WORKLOAD:
        raise ValueOutOfRangeError("Input workload is out of range [0:100]")
    # --- Define ranges
    workload_range = MAX_WORKLOAD - MIN_WORKLOAD
    rpm_range = rpm_max - rpm_min

    # --- Convert the workload range into a 0-1 range
    normalized_workload = (workload - MIN_WORKLOAD) / workload_range

    # --- Convert the normalized range into an rpm
    rpm = rpm_min + (normalized_workload * rpm_range)

    return rpm
