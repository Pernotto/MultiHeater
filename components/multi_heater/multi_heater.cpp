#include "multi_heater.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include <algorithm>
#include <cmath>

namespace esphome {
namespace multi_heater {

static const char *const TAG = "multi_heater";
static const uint32_t LOG_INTERVAL_MS = 10000;

void MultiHeaterClimate::add_element(output::FloatOutput *output, sensor::Sensor *sensor, float max_delta,
                                     float max_temperature, float power, float conductance, float heat_capacity,
                                     bool enabled) {
  this->elements_.push_back(
      Element{output, sensor, max_delta, max_temperature, power, conductance, heat_capacity, enabled});
}

float MultiHeaterClimate::element_limit_(const Element &el, float tw) const {
  // No limit configured: infinity, so the comparisons and the fade below never engage.
  const float limit = std::isnan(el.max_delta) ? INFINITY : tw + el.max_delta;
  return std::isnan(el.max_temperature) ? limit : std::min(limit, el.max_temperature);
}

void MultiHeaterClimate::setup() {
  // Outputs must be off before anything else runs.
  for (auto &el : this->elements_) {
    el.output->set_level(0.0f);
    el.duty = 0.0f;
  }
  this->current_temperature = this->sensor_->state;

  auto restore = this->restore_state_();
  if (restore.has_value()) {
    restore->to_call(this).perform();
  } else {
    this->mode = climate::CLIMATE_MODE_OFF;
    this->target_temperature = this->default_target_temperature_;
  }
  this->last_update_ms_ = millis();
}

void MultiHeaterClimate::control(const climate::ClimateCall &call) {
  if (call.get_mode().has_value())
    this->mode = *call.get_mode();
  if (call.get_target_temperature().has_value())
    this->target_temperature = *call.get_target_temperature();
  if (this->mode == climate::CLIMATE_MODE_OFF) {
    this->all_off_();
    this->integral_ = 0.0f;
    this->action = climate::CLIMATE_ACTION_OFF;
  }
  this->publish_state();
}

climate::ClimateTraits MultiHeaterClimate::traits() {
  auto traits = climate::ClimateTraits();
  traits.add_feature_flags(climate::CLIMATE_SUPPORTS_CURRENT_TEMPERATURE | climate::CLIMATE_SUPPORTS_ACTION);
  traits.set_supported_modes({climate::CLIMATE_MODE_OFF, climate::CLIMATE_MODE_HEAT});
  traits.set_visual_min_temperature(5.0f);
  traits.set_visual_max_temperature(95.0f);
  traits.set_visual_target_temperature_step(0.5f);
  traits.set_visual_current_temperature_step(0.1f);
  return traits;
}

void MultiHeaterClimate::update() {
  const uint32_t now = millis();
  const float dt = std::min((now - this->last_update_ms_) / 1000.0f, 10.0f);
  this->last_update_ms_ = now;

  const float tw = this->sensor_->state;
  if (this->mode == climate::CLIMATE_MODE_OFF) {
    this->all_off_();
    this->integral_ = 0.0f;
    this->publish_if_changed_(climate::CLIMATE_ACTION_OFF, tw);
    return;
  }
  if (std::isnan(tw)) {
    // Fail safe: no water temperature, no heating.
    this->all_off_();
    this->integral_ = 0.0f;
    if (now - this->last_log_ms_ >= LOG_INTERVAL_MS) {
      this->last_log_ms_ = now;
      ESP_LOGW(TAG, "Water temperature unavailable, all elements off");
    }
    this->publish_if_changed_(climate::CLIMATE_ACTION_IDLE, tw);
    return;
  }

  float ta = this->ambient_temperature_;
  if (this->ambient_sensor_ != nullptr && !std::isnan(this->ambient_sensor_->state))
    ta = this->ambient_sensor_->state;
  const float target = this->target_temperature;
  if (this->water_mass_sensor_ != nullptr) {
    const float grams = this->water_mass_sensor_->state;
    if (!std::isnan(grams) && grams > 0.0f)
      this->water_mass_ = grams;  // keep the last valid reading through sensor dropouts
  }
  if (std::isnan(this->water_mass_)) {
    // Fail safe: without a mass the energy needed is unknown.
    this->all_off_();
    this->integral_ = 0.0f;
    if (now - this->last_log_ms_ >= LOG_INTERVAL_MS) {
      this->last_log_ms_ = now;
      ESP_LOGW(TAG, "Water mass unavailable, all elements off");
    }
    this->publish_if_changed_(climate::CLIMATE_ACTION_IDLE, tw);
    return;
  }
  const float mc = this->water_mass_ * WATER_SPECIFIC_HEAT;

  // Heat still stored in the elements will reach the water even if we switch everything off now.
  float e_store = 0.0f;
  float total_avail = 0.0f;
  std::vector<float> avail(this->elements_.size(), 0.0f);
  for (size_t i = 0; i < this->elements_.size(); i++) {
    auto &el = this->elements_[i];
    const float tel = el.sensor->state;
    if (std::isnan(tel)) {
      this->set_duty_(el, 0.0f);
      continue;
    }
    e_store += el.heat_capacity * (tel - tw);
    const float limit = this->element_limit_(el, tw);
    if (!el.enabled || tel >= limit)
      continue;
    // Predictive: steady-state power with the element sitting at its limit.
    float a = std::min(el.power, el.conductance * (limit - tw));
    // Hard limiter: fade to zero over the last limit_band degrees.
    a = std::min(a, el.power * clamp((limit - tel) / this->limit_band_, 0.0f, 1.0f));
    avail[i] = std::max(a, 0.0f);
    total_avail += avail[i];
  }
  float cap = total_avail;
  if (!std::isnan(this->max_power_))
    cap = std::min(cap, this->max_power_);

  // Fill the elements with the most available power first: few hot elements store less heat than many warm ones.
  std::vector<size_t> order(this->elements_.size());
  for (size_t i = 0; i < order.size(); i++)
    order[i] = i;
  std::sort(order.begin(), order.end(), [&avail](size_t a, size_t b) { return avail[a] > avail[b]; });

  // Feed-forward (losses at target) + proportional on the energy still missing.
  const float e_need = mc * (target - tw);
  const float d_target = std::max(target - ta, 0.0f);
  const float p_ff = this->loss_coefficient_ * d_target + this->loss_quadratic_ * d_target * d_target;

  // While holding the target the elements stay warm: that part of their stored heat never
  // reaches the water as a surplus, so only what exceeds it counts as "still to come".
  // Without this the water settles below the target.
  float e_hold = 0.0f;
  float hold_left = std::min(p_ff, cap);
  for (size_t i : order) {
    const auto &el = this->elements_[i];
    const float share = std::min(avail[i], hold_left);
    e_hold += el.heat_capacity * share / el.conductance;
    hold_left -= share;
  }

  const float err = target - tw;
  float p_req = p_ff + (e_need - (e_store - e_hold)) / this->approach_time_ + this->integral_;

  // The integral only removes what the model gets wrong near the target. It stays frozen
  // during the approach from below (it would wind up and cause overshoot) and while saturated.
  if (this->integral_gain_ > 0.0f && err <= this->integral_zone_) {
    const bool saturated_high = p_req >= cap && err > 0.0f;
    const bool saturated_low = p_req <= 0.0f && err < 0.0f;
    if (!saturated_high && !saturated_low)
      this->integral_ = clamp(this->integral_ + this->integral_gain_ * err * dt, -cap, cap);
  }

  const float p_cmd = clamp(p_req, 0.0f, cap);

  float remaining = p_cmd;
  bool heating = false;
  for (size_t i : order) {
    auto &el = this->elements_[i];
    float duty = std::min(avail[i], remaining) / el.power;
    // Pulses shorter than min_duty of the PWM period are not worth a relay cycle: drop a
    // too-short "on" pulse, and close a too-short "off" gap.
    if (duty < this->min_duty_) {
      duty = 0.0f;
    } else if (duty > 1.0f - this->min_duty_) {
      duty = 1.0f;
    }
    this->set_duty_(el, duty);
    remaining -= duty * el.power;
    heating |= duty > 0.0f;
  }

  if (now - this->last_log_ms_ >= LOG_INTERVAL_MS) {
    this->last_log_ms_ = now;
    ESP_LOGD(TAG, "T_w=%.2f target=%.1f P_req=%.0fW (ff=%.0f I=%.0f) E_store=%.0fJ cap=%.0fW cmd=%.0fW", tw, target,
             p_req, p_ff, this->integral_, e_store, cap, this->get_commanded_power());
    for (size_t i = 0; i < this->elements_.size(); i++) {
      auto &el = this->elements_[i];
      ESP_LOGV(TAG, "  [%u] T_el=%.1f avail=%.0fW duty=%.1f%%", (unsigned) i, el.sensor->state, avail[i],
               el.duty * 100.0f);
    }
  }

  this->publish_if_changed_(heating ? climate::CLIMATE_ACTION_HEATING : climate::CLIMATE_ACTION_IDLE, tw);
}

void MultiHeaterClimate::set_duty_(Element &el, float duty) {
  if (std::fabs(duty - el.duty) < 1e-4f)
    return;
  el.duty = duty;
  el.output->set_level(duty);
}

void MultiHeaterClimate::all_off_() {
  for (auto &el : this->elements_)
    this->set_duty_(el, 0.0f);
}

void MultiHeaterClimate::publish_if_changed_(climate::ClimateAction action, float current) {
  const bool same_temp = (std::isnan(current) && std::isnan(this->current_temperature)) ||
                         std::fabs(current - this->current_temperature) < 0.01f;
  if (action == this->action && same_temp)
    return;
  this->action = action;
  this->current_temperature = current;
  this->publish_state();
}

void MultiHeaterClimate::set_element_enabled(size_t index, bool enabled) {
  if (index >= this->elements_.size())
    return;
  auto &el = this->elements_[index];
  el.enabled = enabled;
  if (!enabled)
    this->set_duty_(el, 0.0f);
  ESP_LOGI(TAG, "Element %u %s", (unsigned) index, enabled ? "enabled" : "disabled");
}

bool MultiHeaterClimate::is_element_enabled(size_t index) const {
  return index < this->elements_.size() && this->elements_[index].enabled;
}

float MultiHeaterClimate::get_element_duty(size_t index) const {
  return index < this->elements_.size() ? this->elements_[index].duty : 0.0f;
}

float MultiHeaterClimate::get_commanded_power() const {
  float p = 0.0f;
  for (const auto &el : this->elements_)
    p += el.duty * el.power;
  return p;
}

void MultiHeaterClimate::dump_config() {
  LOG_CLIMATE("", "MultiHeater Climate", this);
  if (this->water_mass_sensor_ != nullptr) {
    ESP_LOGCONFIG(TAG, "  Water mass: sensor (fallback %.0f g)", this->water_mass_);
  } else {
    ESP_LOGCONFIG(TAG, "  Water mass: %.0f g", this->water_mass_);
  }
  ESP_LOGCONFIG(TAG, "  Loss: %.2f W/K + %.4f W/K^2", this->loss_coefficient_, this->loss_quadratic_);
  if (this->ambient_sensor_ != nullptr) {
    ESP_LOGCONFIG(TAG, "  Ambient: sensor (fallback %.1f °C)", this->ambient_temperature_);
  } else {
    ESP_LOGCONFIG(TAG, "  Ambient: %.1f °C", this->ambient_temperature_);
  }
  ESP_LOGCONFIG(TAG, "  Approach time: %.0f s", this->approach_time_);
  if (!std::isnan(this->max_power_))
    ESP_LOGCONFIG(TAG, "  Max power: %.0f W", this->max_power_);
  ESP_LOGCONFIG(TAG, "  Integral gain: %.3f, limit band: %.1f °C, min duty: %.1f %%", this->integral_gain_,
                this->limit_band_, this->min_duty_ * 100.0f);
  ESP_LOGCONFIG(TAG, "  Elements: %u", (unsigned) this->elements_.size());
  for (size_t i = 0; i < this->elements_.size(); i++) {
    const auto &el = this->elements_[i];
    ESP_LOGCONFIG(TAG, "    [%u] P=%.0f W  h=%.2f W/K  C=%.0f J/K  max_delta=%.1f °C  ceiling=%.1f °C  enabled=%s",
                  (unsigned) i, el.power, el.conductance, el.heat_capacity, el.max_delta, el.max_temperature,
                  YESNO(el.enabled));
  }
}

void ElementEnableSwitch::setup() { this->write_state(this->get_initial_state_with_restore_mode().value_or(true)); }

void ElementEnableSwitch::write_state(bool state) {
  this->parent_->set_element_enabled(this->index_, state);
  this->publish_state(state);
}

void ElementEnableSwitch::dump_config() { LOG_SWITCH("", "MultiHeater element enable", this); }

}  // namespace multi_heater
}  // namespace esphome
