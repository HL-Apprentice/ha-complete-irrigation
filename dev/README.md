# Local Home Assistant Test Environment

This folder runs a disposable Home Assistant instance in Docker with
our integration auto-mounted, for **Check 3** of the pre-push protocol
(see [`CONTRIBUTING.md`](../CONTRIBUTING.md)).

## Prerequisites

- Docker Desktop running

## Spin up

From this folder:

```bash
docker compose up -d
```

HA will start on http://localhost:8123 (first start takes ~60 seconds
while it initializes). Browse there, complete the onboarding wizard
(create an admin user — this account lives only in `./ha-config/`,
NOT in your real HA).

## Tail logs

```bash
docker compose logs -f homeassistant
```

Watch for `[complete_irrigation] Setting up Complete Irrigation entry ...`
when you add the integration via the UI.

## Test the integration

1. In the test HA: Settings → Devices & Services → **+ Add Integration**
2. Search for "Complete Irrigation"
3. Walk the config flow:
   - Pick "Manual" (no irrigation integrations are installed in this
     disposable HA)
   - Pick a switch or two from the list (HA's `default_config` does
     not create switches, so you'll get the `no_zones_found` abort —
     that's expected and is itself a test of the abort path)
4. Check the sidebar for the Irrigation panel
5. Check Settings → Devices & Services → Complete Irrigation → Delete
6. Verify HA's icons in Devices & Services still work after delete

## Tear down

```bash
docker compose down              # stops + removes container
docker compose down --volumes    # ALSO wipes ./ha-config/ — clean slate
```

## Iterating on changes

After editing the integration source:

```bash
docker compose restart homeassistant
```

HA picks up changes to `custom_components/complete_irrigation/` on
restart (the directory is mounted read-only into the container).

## Disk layout

```
dev/
├── docker-compose.yml       # this service
├── ha-config/               # HA's runtime state (gitignored except .yaml)
│   ├── configuration.yaml   # committed: minimal HA config
│   ├── .storage/            # HA's data (NOT committed)
│   ├── home-assistant.log
│   └── ...
└── README.md                # this file
```
