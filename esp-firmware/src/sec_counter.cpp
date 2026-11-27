#include "sec.h"
#include <string.h>

#if defined(ESP32)
  #include <Preferences.h>
  #include <esp_system.h>
  RTC_DATA_ATTR static SecRtcBlock g_esp32_rtc_block;
#elif defined(ESP8266)
  #include <Arduino.h>
  #include <Preferences.h>
  extern "C" {
    #include "user_interface.h"
  }
  #define ESP8266_RTC_SLOT_OFFSET 64  // User RTC memory block offset (4-byte words)
#endif

static uint32_t rtc_block_crc(const SecRtcBlock* blk) {
  // CRC covers magic, counter, persist_fail (first 12 bytes)
  return sec_crc32((const uint8_t*)blk, 12);
}

static bool rtc_block_is_valid(const SecRtcBlock* blk) {
  return blk->magic == SEC_RTC_MAGIC && blk->crc == rtc_block_crc(blk);
}

void sec_counter_init_with_ops(SecCounter* sc, const SecCounterOps* ops, void* ctx, uint32_t reserve_step) {
  memset(sc, 0, sizeof(*sc));
  sc->ops = *ops;
  sc->ctx = ctx;
  sc->reserve_step = (reserve_step > 0) ? reserve_step : V2_COUNTER_RESERVE;
  sc->reserved = sc->ops.load_reserved ? sc->ops.load_reserved(sc->ctx) : 0;

  SecRtcBlock rtc;
  memset(&rtc, 0, sizeof(rtc));
  bool is_deepsleep = sc->ops.is_deep_sleep_wake ? sc->ops.is_deep_sleep_wake(sc->ctx) : false;
  bool rtc_loaded = sc->ops.load_rtc ? sc->ops.load_rtc(sc->ctx, &rtc) : false;

  if (is_deepsleep && rtc_loaded && rtc_block_is_valid(&rtc)) {
    sc->counter = rtc.counter;
    sc->persist_fail = rtc.persist_fail;
    if (sc->persist_fail >= 3) {
      sc->panicked = true;
    }
  } else {
    // Cold boot / brownout / garbage RTC / first boot:
    // Strictly greater than any value ever used, tolerating up to 2 undetected failed writes.
    uint32_t jitter = sc->ops.rand_u32 ? (sc->ops.rand_u32(sc->ctx) % sc->reserve_step) : 0;
    sc->counter = sc->reserved + (3U * sc->reserve_step) + 1U + jitter;
    sc->persist_fail = 0;
    // Overwrite any stale RTC block immediately so an old counter can never resurface
    sc->initialized = true;
    sec_counter_prepare_sleep(sc);
  }

  sc->initialized = true;
}

bool sec_counter_next(SecCounter* sc, uint32_t* out_counter) {
  if (!sc->initialized || sc->panicked) {
    return false;
  }

  sc->counter++;

  // Reserve-ahead window check
  if (sc->counter + sc->reserve_step > sc->reserved) {
    uint32_t new_reserved = sc->counter + sc->reserve_step;
    bool ok = false;
    if (sc->ops.save_and_verify_reserved) {
      for (int attempt = 0; attempt < 3; attempt++) {
        if (sc->ops.save_and_verify_reserved(sc->ctx, new_reserved)) {
          ok = true;
          break;
        }
      }
    }

    if (ok) {
      sc->reserved = new_reserved;
      sc->persist_fail = 0;
    } else {
      sc->persist_fail++;
      if (sc->persist_fail >= 3) {
        sc->panicked = true;
        if (sc->ops.on_panic) {
          sc->ops.on_panic(sc->ctx);
        }
        return false;
      }
      // Hard safety invariant: NEVER transmit a counter above what is durably reserved in flash
      if (sc->counter > sc->reserved) {
        sc->counter--;  // Do not consume unpersisted counter
        return false;
      }
    }
  }

  if (out_counter != nullptr) {
    *out_counter = sc->counter;
  }
  return true;
}

void sec_counter_prepare_sleep(SecCounter* sc) {
  if (!sc->initialized || !sc->ops.save_rtc) {
    return;
  }
  SecRtcBlock blk;
  blk.magic = SEC_RTC_MAGIC;
  blk.counter = sc->counter;
  blk.persist_fail = sc->persist_fail;
  blk.crc = rtc_block_crc(&blk);
  sc->ops.save_rtc(sc->ctx, &blk);
}

// ---------------------------------------------------------------------------
// Default platform backends (ESP32 NVS+RTC / ESP8266 Preferences+RTC)
// ---------------------------------------------------------------------------

#if defined(ESP32) || defined(ESP8266)

static uint32_t default_load_reserved(void* ctx) {
  (void)ctx;
  Preferences p;
  if (!p.begin("sec", true)) return 0;
  uint32_t val = p.getUInt("ctr_res", 0);
  p.end();
  return val;
}

static bool default_save_and_verify_reserved(void* ctx, uint32_t value) {
  (void)ctx;
  Preferences p;
  if (!p.begin("sec", false)) return false;
  size_t w = p.putUInt("ctr_res", value);
  uint32_t readback = p.getUInt("ctr_res", 0);
  p.end();
  return (w > 0) && (readback == value);
}

static bool default_load_rtc(void* ctx, SecRtcBlock* out) {
  (void)ctx;
#if defined(ESP32)
  *out = g_esp32_rtc_block;
  return true;
#elif defined(ESP8266)
  return ESP.rtcUserMemoryRead(ESP8266_RTC_SLOT_OFFSET, (uint32_t*)out, sizeof(SecRtcBlock));
#endif
}

static void default_save_rtc(void* ctx, const SecRtcBlock* blk) {
  (void)ctx;
#if defined(ESP32)
  g_esp32_rtc_block = *blk;
#elif defined(ESP8266)
  ESP.rtcUserMemoryWrite(ESP8266_RTC_SLOT_OFFSET, (uint32_t*)blk, sizeof(SecRtcBlock));
#endif
}

static bool default_is_deep_sleep_wake(void* ctx) {
  (void)ctx;
#if defined(ESP32)
  return esp_reset_reason() == ESP_RST_DEEPSLEEP;
#elif defined(ESP8266)
  rst_info* ri = ESP.getResetInfoPtr();
  return ri != nullptr && ri->reason == REASON_DEEP_SLEEP_AWAKE;
#endif
}

static uint32_t default_rand_u32(void* ctx) {
  (void)ctx;
  return sec_random_u32();
}

void sec_counter_init_default(SecCounter* sc) {
  SecCounterOps ops;
  ops.load_reserved = default_load_reserved;
  ops.save_and_verify_reserved = default_save_and_verify_reserved;
  ops.load_rtc = default_load_rtc;
  ops.save_rtc = default_save_rtc;
  ops.is_deep_sleep_wake = default_is_deep_sleep_wake;
  ops.rand_u32 = default_rand_u32;
  ops.on_panic = nullptr;
  sec_counter_init_with_ops(sc, &ops, nullptr, V2_COUNTER_RESERVE);
}

#endif
