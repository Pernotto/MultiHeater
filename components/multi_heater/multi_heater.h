#pragma once

#include "esphome/core/component.h"
#include "esphome/components/climate/climate.h"
#include "esphome/components/output/float_output.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/switch/switch.h"
#include <vector>

namespace esphome {
namespace multi_heater {

static constexpr float WATER_SPECIFIC_HEAT = 4.186f;  // J/(g*K)

struct Element {
  output::FloatOutput *output;
  sensor::Sensor *sensor;
  float max_delta;        // degC the element may be above the water
  float max_temperature;  // degC, optional absolute ceiling (NAN = none)
  float power;            // W at 100 % duty (P_full)
  float conductance;      // W/K element -> water (h)
  float heat_capacity;    // J/K (C_el)
  bool enabled;
  float duty{0.0f};
};

class MultiHeaterClimate : public climate::Climate, public PollingComponent {
 public:
  MultiHeaterClimate() : PollingComponent(1000) {}

  void setup() override;
  void update() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::HARDWARE; }

  void set_sensor(sensor::Sensor *sensor) { this->sensor_ = sensor; }
  void set_ambient_sensor(sensor::Sensor *sensor) { this->ambient_sensor_ = sensor; }
  void set_ambient_temperature(float t) { this->ambient_temperature_ = t; }
  void set_default_target_temperature(float t) { this->default_target_temperature_ = t; }
  void set_water_mass(float grams) { this->water_mass_ = grams; }
  void set_water_mass_sensor(sensor::Sensor *sensor) { this->water_mass_sensor_ = sensor; }
  void set_loss_coefficient(float k) { this->loss_coefficient_ = k; }
  void set_loss_quadratic(float k2) { this->loss_quadratic_ = k2; }
  void set_approach_time(float seconds) { this->approach_time_ = seconds; }
  void set_max_power(float w) { this->max_power_ = w; }
  void set_integral_gain(float ki) { this->integral_gain_ = ki; }
  void set_integral_zone(float zone) { this->integral_zone_ = zone; }
  void set_limit_band(float band) { this->limit_band_ = band; }
  void set_min_duty(float duty) { this->min_duty_ = duty; }
  void add_element(output::FloatOutput *output, sensor::Sensor *sensor, float max_delta, float max_temperature,
                   float power, float conductance, float heat_capacity, bool enabled);

  void set_element_enabled(size_t index, bool enabled);
  bool is_element_enabled(size_t index) const;
  // Commanded duty 0..1 of an element, for display lambdas.
  float get_element_duty(size_t index) const;
  // Total commanded power, sum(duty * P_full), in W.
  float get_commanded_power() const;

 protected:
  void control(const climate::ClimateCall &call) override;
  climate::ClimateTraits traits() override;

  void set_duty_(Element &el, float duty);
  void all_off_();
  // Highest element temperature allowed at this water temperature.
  float element_limit_(const Element &el, float tw) const;
  void publish_if_changed_(climate::ClimateAction action, float current);

  sensor::Sensor *sensor_{nullptr};
  sensor::Sensor *ambient_sensor_{nullptr};
  float ambient_temperature_{20.0f};
  float default_target_temperature_{60.0f};
  sensor::Sensor *water_mass_sensor_{nullptr};
  float water_mass_{NAN};  // grams: fixed value, then the last valid sensor reading
  float loss_coefficient_{0.0f};  // W/K
  float loss_quadratic_{0.0f};    // W/K^2, for vessels whose loss grows faster than dT (evaporation)
  float approach_time_{300.0f};
  float max_power_{NAN};
  float integral_gain_{0.0f};   // W/(K*s)
  float integral_zone_{1.0f};   // degC below the target from which the integral is active
  float limit_band_{5.0f};
  float min_duty_{0.02f};

  float integral_{0.0f};
  uint32_t last_update_ms_{0};
  uint32_t last_log_ms_{0};
  std::vector<Element> elements_;
};

class ElementEnableSwitch : public switch_::Switch, public Component {
 public:
  ElementEnableSwitch(MultiHeaterClimate *parent, size_t index) : parent_(parent), index_(index) {}
  void setup() override;
  void dump_config() override;

 protected:
  void write_state(bool state) override;

  MultiHeaterClimate *parent_;
  size_t index_;
};

}  // namespace multi_heater
}  // namespace esphome
