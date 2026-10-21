import esphome.codegen as cg
from esphome.components import climate, output, sensor, switch
import esphome.config_validation as cv
from esphome.const import CONF_OUTPUT, CONF_SENSOR

DEPENDENCIES = ["sensor", "output"]
AUTO_LOAD = ["switch"]

CONF_AMBIENT_SENSOR = "ambient_sensor"
CONF_AMBIENT_TEMPERATURE = "ambient_temperature"
CONF_APPROACH_TIME = "approach_time"
CONF_CONDUCTANCE = "conductance"
CONF_DEFAULT_TARGET_TEMPERATURE = "default_target_temperature"
CONF_ELEMENTS = "elements"
CONF_ENABLE_SWITCH = "enable_switch"
CONF_ENABLED = "enabled"
CONF_HEAT_CAPACITY = "heat_capacity"
CONF_INTEGRAL_GAIN = "integral_gain"
CONF_INTEGRAL_ZONE = "integral_zone"
CONF_LIMIT_BAND = "limit_band"
CONF_LOSS_COEFFICIENT = "loss_coefficient"
CONF_LOSS_QUADRATIC = "loss_quadratic"
CONF_MAX_DELTA = "max_delta"
CONF_MAX_POWER = "max_power"
CONF_MAX_TEMPERATURE = "max_temperature"
CONF_MIN_DUTY = "min_duty"
CONF_POWER = "power"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_WATER_MASS = "water_mass"
CONF_WATER_MASS_SENSOR = "water_mass_sensor"

multi_heater_ns = cg.esphome_ns.namespace("multi_heater")
MultiHeaterClimate = multi_heater_ns.class_(
    "MultiHeaterClimate", climate.Climate, cg.PollingComponent
)
ElementEnableSwitch = multi_heater_ns.class_(
    "ElementEnableSwitch", switch.Switch, cg.Component
)

ELEMENT_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.Required(CONF_OUTPUT): cv.use_id(output.FloatOutput),
            cv.Required(CONF_TEMPERATURE_SENSOR): cv.use_id(sensor.Sensor),
            # Optional limits. max_delta: the element may be at most this much hotter than the
            # water. max_temperature: absolute ceiling. With neither, the element is not limited.
            cv.Optional(CONF_MAX_DELTA): cv.positive_not_null_float,
            cv.Optional(CONF_MAX_TEMPERATURE): cv.temperature,
            cv.Required(CONF_POWER): cv.positive_not_null_float,
            cv.Required(CONF_CONDUCTANCE): cv.positive_not_null_float,
            cv.Required(CONF_HEAT_CAPACITY): cv.positive_not_null_float,
            cv.Optional(CONF_ENABLED): cv.boolean,
            cv.Optional(CONF_ENABLE_SWITCH): switch.switch_schema(
                ElementEnableSwitch, default_restore_mode="RESTORE_DEFAULT_ON"
            ).extend(cv.COMPONENT_SCHEMA),
        }
    ),
    # With a switch, its restore_mode decides the initial state.
    cv.has_at_most_one_key(CONF_ENABLED, CONF_ENABLE_SWITCH),
)

CONFIG_SCHEMA = cv.All(
    climate.climate_schema(MultiHeaterClimate)
    .extend(
        {
            cv.Required(CONF_SENSOR): cv.use_id(sensor.Sensor),
            # Grams. The sensor is used when it has a valid reading; the fixed value is the
            # fallback (and the only source when no sensor is given).
            cv.Optional(CONF_WATER_MASS): cv.positive_not_null_float,
            cv.Optional(CONF_WATER_MASS_SENSOR): cv.use_id(sensor.Sensor),
            # Vessel loss = loss_coefficient * dT + loss_quadratic * dT^2, dT = water - ambient.
            cv.Required(CONF_LOSS_COEFFICIENT): cv.positive_float,
            cv.Optional(CONF_LOSS_QUADRATIC, default=0.0): cv.positive_float,
            cv.Optional(CONF_AMBIENT_SENSOR): cv.use_id(sensor.Sensor),
            cv.Optional(CONF_AMBIENT_TEMPERATURE, default=20.0): cv.temperature,
            cv.Optional(CONF_DEFAULT_TARGET_TEMPERATURE, default=60.0): cv.temperature,
            cv.Optional(
                CONF_APPROACH_TIME, default="300s"
            ): cv.positive_time_period_seconds,
            cv.Optional(CONF_MAX_POWER): cv.positive_not_null_float,
            cv.Optional(CONF_INTEGRAL_GAIN, default=0.0): cv.positive_float,
            cv.Optional(CONF_INTEGRAL_ZONE, default=1.0): cv.positive_float,
            cv.Optional(CONF_LIMIT_BAND, default=5.0): cv.positive_not_null_float,
            cv.Optional(CONF_MIN_DUTY, default="2%"): cv.percentage,
            cv.Required(CONF_ELEMENTS): cv.All(
                cv.ensure_list(ELEMENT_SCHEMA), cv.Length(min=1)
            ),
        }
    )
    .extend(cv.polling_component_schema("1s")),
    cv.has_at_least_one_key(CONF_WATER_MASS, CONF_WATER_MASS_SENSOR),
)


async def to_code(config):
    var = await climate.new_climate(config)
    await cg.register_component(var, config)

    sens = await cg.get_variable(config[CONF_SENSOR])
    cg.add(var.set_sensor(sens))
    if CONF_AMBIENT_SENSOR in config:
        amb = await cg.get_variable(config[CONF_AMBIENT_SENSOR])
        cg.add(var.set_ambient_sensor(amb))
    cg.add(var.set_ambient_temperature(config[CONF_AMBIENT_TEMPERATURE]))
    cg.add(var.set_default_target_temperature(config[CONF_DEFAULT_TARGET_TEMPERATURE]))
    if CONF_WATER_MASS in config:
        cg.add(var.set_water_mass(config[CONF_WATER_MASS]))
    if CONF_WATER_MASS_SENSOR in config:
        mass_sens = await cg.get_variable(config[CONF_WATER_MASS_SENSOR])
        cg.add(var.set_water_mass_sensor(mass_sens))
    cg.add(var.set_loss_coefficient(config[CONF_LOSS_COEFFICIENT]))
    cg.add(var.set_loss_quadratic(config[CONF_LOSS_QUADRATIC]))
    cg.add(var.set_approach_time(config[CONF_APPROACH_TIME].total_seconds))
    if CONF_MAX_POWER in config:
        cg.add(var.set_max_power(config[CONF_MAX_POWER]))
    cg.add(var.set_integral_gain(config[CONF_INTEGRAL_GAIN]))
    cg.add(var.set_integral_zone(config[CONF_INTEGRAL_ZONE]))
    cg.add(var.set_limit_band(config[CONF_LIMIT_BAND]))
    cg.add(var.set_min_duty(config[CONF_MIN_DUTY]))

    for i, el in enumerate(config[CONF_ELEMENTS]):
        out = await cg.get_variable(el[CONF_OUTPUT])
        el_sens = await cg.get_variable(el[CONF_TEMPERATURE_SENSOR])
        cg.add(
            var.add_element(
                out,
                el_sens,
                el.get(CONF_MAX_DELTA, float("nan")),
                el.get(CONF_MAX_TEMPERATURE, float("nan")),
                el[CONF_POWER],
                el[CONF_CONDUCTANCE],
                el[CONF_HEAT_CAPACITY],
                el.get(CONF_ENABLED, True),
            )
        )
        if sw_conf := el.get(CONF_ENABLE_SWITCH):
            sw = await switch.new_switch(sw_conf, var, i)
            await cg.register_component(sw, sw_conf)
