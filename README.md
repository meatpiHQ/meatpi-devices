# MeatPi Device Catalog

The living source of **device-type definitions** for the
[MeatPi Home Assistant integration](https://github.com/meatpiHQ/ha-wican).

Every Home Assistant install running the integration fetches
[`device_catalog.json`](device_catalog.json) from this repository at most
once per 24 hours (users can force it with the integration's
*Refresh integration definitions* button). Merging a new device type here
gives it first-class support — correct model name, capability gating,
product-specific sensors — on every install, **without an integration
release**.

> ⚠️ **This file ships to every install.** A structurally broken document
> is rejected as a whole and installs keep their previous copy — a bad
> merge degrades *silently*, not loudly. `validate_catalog.py` runs in CI
> on every PR and fails on anything the integration would drop; do not
> merge red.

## Adding a device type

1. Add an entry under `device_types` (see the schema below).
2. Run `python3 validate_catalog.py` locally — it must print `OK`.
3. Open a PR. CI runs the same validator.
4. After merge, installs pick it up within 24 h (or instantly via the
   refresh button). The integration matches devices to the entry by the
   `device_type` mDNS TXT record, or by `hw_keywords` against the
   device-reported `hw_version`.

## Schema (version 1)

```jsonc
{
  "schema": 1,                     // required, exactly 1
  "device_types": {
    "my_product": {                // slug: ^[a-z][a-z0-9_]{0,31}$
      "model": "My Product",       // required, ≤64 chars — shown in HA device registry
      "hw_keywords": ["myproduct"],// ≤8 lowercase keywords, ≤32 chars each;
                                   // matched as tokens against hw_version
      "supports_obd_pids": false,  // gates OBD/ECU entities
      "supports_gps": true,        // gates the device_tracker
      "firmware": {                // or null; not consumed by the integration yet
        "repo": "meatpiHQ/my-fw",  // owner/repo
        "asset_pattern": "my-fw_*.bin"
      },
      "sensors": [                 // ≤32 product-specific sensors
        {
          "key": "lte_rssi",             // required; key in the pushed "status" object
                                          // ^[A-Za-z0-9_.-]{1,64}$
          "name": "LTE signal",           // display name (defaults to key)
          "device_class": "signal_strength", // optional HA sensor device class
          "unit": "dBm",                  // optional, ≤32 chars
          "icon": "mdi:signal",           // optional, must start with "mdi:"
          "diagnostic": true              // default true (Diagnostics section)
        }
      ]
    }
  }
}
```

Bounds (enforced by the integration and by CI): ≤64 device types, file
≤256 KiB. **Reserved slugs** — `wican`, `wican_usb`, `wican_pro`,
`meatpi` — are defined in integration code and cannot appear here.
Catalog entries override same-named non-reserved built-ins (e.g.
`espnetlink`), which act as the integration's offline fallback.

## How the integration consumes this

- Fetch: 24 h cadence, 15 s timeout, 256 KiB cap, streamed read.
- A valid document applies immediately and is persisted in HA `.storage`
  (which wins over the integration's bundled fallback copy at startup).
- Individually invalid entries are dropped; a structurally invalid
  document (wrong `schema`, non-object `device_types`) is ignored whole.
- Full spec and publishing workflow:
  [`notes/device-contract/DEVICE_CATALOG.md`](https://github.com/meatpiHQ/ha-wican/blob/main/notes/device-contract/DEVICE_CATALOG.md)
  in the integration repo. Keep the integration's bundled
  `data/device_catalog.json` in sync when cutting integration releases.
