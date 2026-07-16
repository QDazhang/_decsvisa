def set_mc_temperature(decs, temperature_K):
    """Set MC Temperature in K."""
    return decs.query(f"set_MC_T:{temperature_K}")


def set_mc_heater_power(decs, power_w):
    """Set MC heater open-loop power in W."""
    return decs.query(f"set_MC_H:{power_w}")


def get_still_temperature(decs, temperature_K):
    """Get Still temperature in K."""
    return decs.query(f"get_STILL_T:{temperature_K}")


def set_still_heater_power(decs, power_w):
    """Set Still heater open-loop power in W."""
    return decs.query(f"set_STILL_H:{power_w}")


def mc_heater_off(decs):
    return decs.query("set_MC_H_OFF:0")


def still_heater_off(decs):
    return decs.query("set_STILL_H_OFF:0")


def all_heaters_off(decs):
    mc_heater_off(decs)
    still_heater_off(decs)