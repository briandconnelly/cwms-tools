# CWMS — an overview for tool builders

This document is a self-contained orientation to the U.S. Army Corps of
Engineers' Corps Water Management System (CWMS), aimed at agents
building tools on top of it. It covers what CWMS is, where to find the
canonical sources, the data model, naming conventions, query patterns,
and the gotchas you will hit. It deliberately does not assume any
specific use case — the goal is for a reader to come away knowing
*where to find things*, *how to query things*, *what entities and
measures exist*, and *what mistakes to avoid*.

---

## 1. Orientation

**CWMS** (Corps Water Management System) is the U.S. Army Corps of
Engineers' platform for operating and reporting on the federal water
resources it manages: mainstem reservoirs, hydropower projects,
flood-control dams, navigation locks, and environmental monitoring
stations. USACE has run CWMS as an internal Oracle-backed system for
decades; in recent years they have stood up a public REST API on top
of it (the CWMS Data API, or CDA) that exposes a read-only slice for
non-USACE consumers.

The data is hydrologic time-series and operational metadata: pool
elevations, inflow/outflow rates, storage, energy generation, water
quality, weather, and the thresholds (flood stages, conservation pool
limits, rule curves) that give those numbers operational meaning.

**Scope.** CDA covers USACE-operated facilities and CWMS-affiliated
sensors. It is **not** a general water-data API: it does not include
USGS NWIS-only gages, NOAA NWS forecasts (except where mirrored), or
non-USACE dams. For non-USACE data, use the relevant agency's own
API.

---

## 2. The stack — where to find things

| Layer | URL / location | What's there |
|---|---|---|
| Live API | https://cwms-data.usace.army.mil/cwms-data/ | The public CDA REST API (read endpoints unauthenticated; write requires auth) |
| Swagger UI | https://cwms-data.usace.army.mil/cwms-data/swagger-ui.html | Interactive endpoint browser |
| Read-the-Docs | https://cwms-data-api.readthedocs.io/ | Narrative documentation (index works; deep links sometimes 404) |
| CDA source | https://github.com/USACE/cwms-data-api | Java implementation. **The DTOs here are the canonical schemas.** |
| Canonical DTOs | `cwms-data-api/src/main/java/cwms/cda/data/dto/` | Field-level ground truth for every entity |
| Python wrapper | https://github.com/HydrologicEngineeringCenter/cwms-python | Official Python client (`pip install cwms-python`) |
| Schema installer | Docker image (referenced in CDA repo) | For spinning up a local CWMS DB for testing |

When the readthedocs and Swagger pages are incomplete or out of date
(both happen), the Java DTOs and the Python wrapper's source are the
authoritative references.

---

## 3. Mental model — three things to internalize

Three non-obvious things shape every query you will write.

### 3.1 CWMS is a federation of publishers

The single most important insight. The data layer is keyed by
**publisher**, not by office or location. Each timeseries' identifier
ends in a *version segment* (e.g. `Best-MRBWM`, `CBT-REV`,
`IRIDIUM-RAW`, `Ccp-Rev`, `MVDhist-rev`, `MANUAL`) that names an
operational team, computation pipeline, or sensor relay that produced
the series. **Publishers cluster geographically and functionally but
cross-cut the office hierarchy.** The publisher is the single most
informative signal about a record: it tells you whether the series is
live, what other parameters likely exist nearby, and which
operational team is the authority.

See §6.3 for the publisher zoo.

### 3.2 The catalog is an index of *places*, not data

Many catalog records are **ghosts** — they exist as catalog entries
but carry no timeseries. Ghosts are pervasive:

- ~100% of records in NW Division *district* offices (NWO, NWK, NWS,
  NWP, NWW) are catalog stubs with no data
- ~95% of depth-tag/unit-tag sub-locations under major projects
  return zero timeseries
- A single facility (Bear Creek Dam, Fort Peck, Oahe) typically has
  5–20 catalog variants; usually 0–2 of them actually publish data

A successful name match in the catalog does not imply usable data.
Every navigation step needs to filter for publishers (i.e. ts_id
presence).

### 3.3 The office that owns a location ≠ the office where its data lives

The USACE org chart is real and load-bearing, but data publication
follows operational responsibility, not administrative ownership. In
particular:

- **NW Division publishes its mainstem reservoir and hydropower data
  at the regional level** (`NWDM` for Missouri, `NWDP` for Pacific
  Northwest), even though individual dams are administratively owned
  by district offices (`NWO`, `NWK`, `NWS`, `NWP`, `NWW`). District
  offices in NWD are essentially empty stubs in CDA.
- **Other divisions** (MVD, SWD, LRD, etc.) publish at the district
  level. Coverage richness within a single district varies wildly
  (see §6.3 — Tulsa District publishes 31-param flood-control
  reservoirs alongside zero-param locks).

When searching by office, knowing whether you're in an NW-style
rollup vs a district-publishing division saves a lot of empty
queries.

---

## 4. Core entities

CWMS organizes around five entities. Each has a canonical DTO at
`cwms-data-api/src/main/java/cwms/cda/data/dto/`.

| Entity | DTO | Role |
|---|---|---|
| Office | `Office.java` | USACE org unit; partitions the catalog |
| Location | `Location.java` (+ `project/Project.java` specialization) | A named place |
| Timeseries | `TimeSeriesIdentifierDescriptor.java`, `TimeSeries.java` | A sampled series of values |
| Location Level | `locationlevel/LocationLevel.java` (+ subclasses) | A threshold/target/reference value |
| Forecast | `forecast/ForecastSpec.java`, `forecast/ForecastInstance.java` | A model-produced prediction |

Plus a supporting vocabulary entity:

| | | |
|---|---|---|
| Parameter | `Parameter.java` | The measured quantity (e.g. `Elev`, `Flow-In`) |

### 4.1 Office

The USACE org chart. ~68 offices, organized by `type` and `reportsTo`.

```
corps headquarters (HQ)
├── division headquarters (NWD, MVD, SWD, LRD, NAD, SAD, SPD, POD)
│   ├── division regional (NWDM, NWDP)        — present in NWD; absent in most others
│   │   └── district (NWO, NWK, NWS, NWP, NWW)  — in NWD these are empty stubs
│   └── district (MVS, MVK, MVP, ...)            — direct under division in non-NW
└── field operating activity (ERD, HEC, IWR, ...)
```

Fields (from `Office` DTO):

| Field | Meaning |
|---|---|
| `name` | Office code (e.g. `NWO`, `NWDM`, `MVS`) |
| `longName` | Full name |
| `type` | One of: `unknown` \| `corps headquarters` \| `division headquarters` \| `division regional` \| `district` \| `field operating activity` |
| `reportsTo` | Parent office code |

Endpoint: `GET /offices` (optionally filtered by region).

### 4.2 Location (and Project)

A row in a per-office catalog. Identified by `(officeId, name)`.

Fields (from `Location` DTO):

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Location id within the office |
| `officeId` | yes | Administratively-owning office |
| `boundingOfficeId` |   | Geographic-containing district |
| `timezoneName` | yes | IANA timezone (e.g. `America/Chicago`) |
| `horizontalDatum` | yes | Geodetic reference for lat/lon (NAD27, NAD83, WGS84) |
| `locationKind` | yes | One of: `PROJECT`, `SITE`, `STREAM`, `STREAM_LOCATION`, `LOCK`, `OUTLET` |
| `locationType` |   | Looser free-text classification |
| `active` |   | Operational flag (default true) |
| `latitude`, `longitude` |   | Computed/measured coordinates |
| `publishedLatitude`, `publishedLongitude` |   | Official published coordinates (often differ from computed) |
| `verticalDatum`, `elevation`, `elevationUnits` |   | Elevation reference + value |
| `nation`, `stateInitial`, `countyName`, `nearestCity` |   | Political geography |
| `publicName`, `longName`, `description`, `mapLabel` |   | Human-facing names |
| `aliases` |   | Alternate names |

Two non-obvious things:

1. **Published vs computed coordinates.** Both pairs can be present.
   Use `publishedLatitude`/`Longitude` for citation; `latitude`/
   `longitude` for raw computed positions.
2. **Datums are required metadata.** A lat/lon without
   `horizontalDatum` is ambiguous at the meter scale. Preserve the
   datum in any spatial join.

Locations form a parent/child hierarchy via id-prefix convention
(`FTPK` → `FTPK1`, `FTPK-bl_7000`, ...). No explicit `parent_ref`
field — the prefix *is* the hierarchy.

**Endpoints:** `GET /locations`, `GET /locations/{name}`, plus
`GET /catalog/LOCATION` for paginated search.

#### Project — Location specialization

A Project is a Location plus operational metadata. DTO at
`dto/project/Project.java`.

| Field | Meaning |
|---|---|
| `location` | The underlying Location |
| `federalCost`, `nonFederalCost`, `costYear`, `costUnit` | Construction cost breakdown |
| `federalOAndMCost`, `nonFederalOAndMCost` | O&M costs |
| `authorizingLaw` | Statute that authorized the project |
| `projectOwner` | Owning entity |
| `hydropowerDesc`, `sedimentationDesc`, `downstreamUrbanDesc`, `bankFullCapacityDesc` | Free-text operational descriptions |
| `pumpBackLocation`, `nearGageLocation` | Linked location references |
| `yieldTimeFrameStart`, `yieldTimeFrameEnd` | Operational evaluation period |
| `projectRemarks` | Free-text notes |

Useful for "tell me about this dam" questions that can't be answered
from Location alone.

**Endpoint:** `GET /projects/{office}/{name}`.

### 4.3 Timeseries

Named by a six-segment dotted string — the **ts_id**:

```
Location.Parameter.Type.Interval.Duration.Version
```

| Segment | Examples | Meaning |
|---|---|---|
| Location | `FTPK`, `GWLW_S1-D3,0ft`, `Carlyle Lk` | Location id |
| Parameter | `Elev`, `Flow-In`, `Temp-Water`, `Conc-DissolvedOxygen` | Measured quantity |
| Type | `Inst`, `Ave`, `Total`, `Const` | Instantaneous, average, sum, constant |
| Interval | `1Hour`, `~1Day`, `15Minutes`, `1Month`, `0` | Reporting cadence. `~` = irregular |
| Duration | `0`, `1Hour`, `1Day`, `8Hours` | Aggregation window (meaningful for `Ave`/`Total`) |
| Version | `Best-MRBWM`, `CBT-REV`, `IRIDIUM-RAW`, `Computed`, `MANUAL` | **The publisher** (see §6.3) |

Reading a ts_id at a glance:

```
FOSS.Elev.Inst.15Minutes.0.Ccp-Rev
└┬─┘ └─┬┘ └┬─┘ └───┬────┘ └┬┘ └──┬───┘
 │     │   │       │       │     └── Publisher: Tulsa District, revised
 │     │   │       │       └──────── Duration: 0 (instantaneous)
 │     │   │       └──────────────── Interval: 15-min reporting
 │     │   └──────────────────────── Type: instantaneous reading
 │     └──────────────────────────── Parameter: elevation
 └────────────────────────────────── Location: Foss Reservoir, OK
```

Each ts has an **identifier descriptor** with metadata around the
bare ts_id string (from `TimeSeriesIdentifierDescriptor` DTO):

| Field | Meaning |
|---|---|
| `timeSeriesId` | The 6-segment string |
| `officeId` | Owning office |
| `timezoneName` | IANA timezone |
| `intervalOffsetMinutes` | Offset within the interval (e.g. hourly series sampled at :15 has `intervalOffsetMinutes=15`) |
| `active` | Whether the ts is currently active |
| `aliases` | Alternate ts_id strings |

**Endpoints:**
- `GET /timeseries?name={ts_id}&office={office}&begin=...&end=...&unit=EN`
- `GET /timeseries/recent/{group}` for the most-recent N values
- `GET /catalog/TIMESERIES` for paginated catalog search
- `GET /timeseries/identifier-descriptor` family for the descriptor metadata
- Plus storage/group/binary/profile/text variants (see §7.1)

### 4.4 Location Level

A threshold, target, or reference value for a parameter at a
location. Provides operational context: "current pool is 21.88 ft"
becomes "21.88 ft — 0.1 ft above seasonal target, 2 ft below top of
conservation pool."

CWMS represents levels in three layers:

1. **Specified Level** (office-scoped vocabulary). A named *type* of
   level — "Top of Conservation Pool", "Flood Stage", "Spillway
   Crest", "Rule Curve", etc. Each office maintains its own
   vocabulary. Endpoint: `GET /specified-levels`.

2. **Location Level** (the configuration). A specific instance of a
   Specified Level applied to a (location, parameter), with an
   effective date and optional expiration. **Composite key:
   `level_id + office + effective_date`** — newer configurations
   supersede older ones. Endpoint: `GET /levels`.

3. **Level Values** (the data). The actual numbers, in one of four
   mutually exclusive varieties.

**Location Level fields** (from `LocationLevel` DTO):

| Field | Meaning |
|---|---|
| `locationLevelId` | Composite id: `<location>.<parameter>.<parameter_type>.<duration>.<specified_level_id>` |
| `officeId` | Owning office |
| `levelDate` | Effective date (required) |
| `expirationDate` | When this configuration ends |
| `specifiedLevelId` | The level *type* (vocabulary term) |
| `parameterId`, `parameterTypeId`, `durationId`, `levelUnitsId` | What's being measured + units |
| `interpolateString` | `T`/`F` — interpolate between values? |
| `attributeValue` + `attributeUnitsId` + `attributeParameterId` + `attributeParameterTypeId` + `attributeDurationId` | Optional secondary-axis attribute (rating-curve-like parameterization) |
| `aliases`, `levelComment` | Auxiliary |

**Level value varieties:**

| Variety | Extra fields | When used |
|---|---|---|
| Constant | `levelValue` (single value) | Fixed structural thresholds (spillway crest) |
| Seasonal (`SeasonalLocationLevel`) | `intervalOrigin`, `intervalMonths` xor `intervalMinutes`, `seasonalValues: List<{value, offsetMonths, offsetMinutes}>` | Annual rule curves, seasonal flood pools |
| TimeSeries (`TimeSeriesLocationLevel`) | `seasonalTimeSeriesId` (a ts_id) | Dynamic guide curves driven by forecast |
| Virtual (`VirtualLocationLevel`) | `constituents`, `constituentConnections` | Computed from multiple inputs |

**Endpoints:**
- `GET /levels` — search by `level-id-mask`, `office`, `unit`, `datum`, `begin`, `end`
- `GET /levels/{level_id}?office=...&effective-date=...&unit=...` — fetch one
- `GET /levels/{level_id}/timeseries?office=...&interval=...&begin=...&end=...` — **converts any level variety into a uniform timeseries representation.** This is the keystone endpoint for value-with-context queries; it eliminates client-side branching on variety.
- `GET /specified-levels?specified-level-mask=...&office=...` — list vocabulary

### 4.5 Forecast

Model-produced predictions. CWMS represents forecasts in two
entities — a *spec* (the configuration) and an *instance* (one run).

**Forecast Spec** (`ForecastSpec` DTO):

| Field | Meaning |
|---|---|
| `specId` | Spec key |
| `officeId` | Owning office |
| `designator` | Subkey within the spec |
| `locationId` | The location being forecast |
| `sourceEntityId` | The model/agency/team producing the forecast |
| `description` | Free-text |
| `timeSeriesIds: List<String>` | **Full ts_ids that hold the forecast values** |

**Forecast Instance** (`ForecastInstance` DTO):

| Field | Meaning |
|---|---|
| `spec` | The associated `ForecastSpec` |
| `dateTime` | Reference timestamp |
| `issueDateTime` | When this run was generated |
| `firstDateTime`, `lastDateTime` | Forecast valid period (horizon) |
| `maxAge` | How long the instance remains valid |
| `notes`, `metadata` | Free-text + arbitrary key-value |
| `filename`, `fileMediaType`, `fileData`, `fileDataUrl`, `fileDescription` | Optional binary attachment |

Predicted values live either:
- **In linked ts_ids** — `spec.timeSeriesIds` points to regular
  timeseries that carry forecast values (publisher conventions
  parallel to observations)
- **In file attachments** — `fileData`/`fileDataUrl` ships a
  structured file (often DSS, XML, or JSON depending on
  `fileMediaType`)

**Endpoints:**
- `GET /forecast-spec?office=...&designator=...&location-id=...`
- `GET /forecast-spec/{spec-id}?office=...`
- `GET /forecast-instance?office=...&name={spec-id}&designator=...&forecast-date-begin=...&forecast-date-end=...`
- `GET /forecast-instance/{name}?office=...`

The `name` parameter ambiguity (sometimes spec id, sometimes instance
id) is a real CDA wart — always include `office` and at least one of
`designator` or `name` to disambiguate.

### 4.6 Parameter vocabulary

The `Parameter` DTO is office-scoped (in principle; standardized in
practice).

| Field | Meaning |
|---|---|
| `name` | Full parameter code (e.g. `Elev`, `Elev-Forebay`, `Flow-Out`) |
| `baseParameter` | Root (e.g. `Elev`, `Flow`, `Temp`) |
| `subParameter` | Specialization (e.g. `Forebay`, `Out`, `Water`) |
| `subParameterDescription` | Human-readable description of the sub |
| `dbUnitId` | Default storage unit |
| `unitLongName`, `unitDescription` | Unit metadata |
| `dbOfficeId` | Owning office |

**Naming convention:** parameter codes follow `Base[-Sub]`. Split on
the *first* hyphen to get base+sub; further hyphens belong to the sub
portion (e.g. `%-Conservation Pool Full` is base `%` + sub
`Conservation Pool Full`).

Endpoint: `GET /parameters?office=...`.

---

## 5. Common parameter codes

Case-sensitive. Second segment of ts_id.

### Hydrologic primary

| Parameter | Meaning | Typical units (EN / SI) |
|---|---|---|
| `Elev` | Pool elevation | ft / m |
| `Elev-Forebay` | Upstream of dam | ft / m |
| `Elev-Tailwater` | Downstream of dam | ft / m |
| `Elev-RuleCurve` | Target elevation | ft / m |
| `Stage` | Gage height | ft / m |
| `Flow-In` | Inflow | cfs / cms |
| `Flow-Out` | Outflow | cfs / cms |
| `Flow-Gen` | Generation flow | cfs / cms |
| `Flow-Spill` | Spillway flow | cfs / cms |
| `Flow-Res In/Out` | District-format reservoir flows | cfs / cms |
| `Flow-Pump Out` | Pumped-storage outflow | cfs / cms |
| `Stor` | Total reservoir storage | acre-ft / m³ |
| `Stor-Conservation Pool`, `Stor-Flood Pool` | Storage zones | acre-ft / m³ |
| `Volume-Res In/Out` | Accumulated volumes | acre-ft / m³ |
| `%-Conservation Pool Full`, `%-Flood Pool Full` | Pool fill percent | % |

### Hydropower / weather / WQ

| Category | Codes |
|---|---|
| Hydropower | `Energy` (MWh) |
| Precipitation | `Precip`, `Precip-Cum`, `Precip-Cuml`, `Precip-Inc`, `Precip-Mean Areal` (in / mm) |
| Weather | `Temp-Air` (°F / °C), `Dir-Wind` (deg), `Pres-Air` (kPa), `Evap` (in / mm) |
| Water quality | `Temp-Water`, `Conc-DissolvedOxygen` (mg/l), `Conc-Salinity`, `Cond` (µmho/cm), `%-Saturation-TDG`, `Pres-Water-TotalGas`, `Depth-WQSensors` (mm) |
| Project state | `Code-ProjectStatus`, `Code-Evap Type`, `Volt-Battery`, `Volt-Battery Load` |

CDA stores most series in metric; English-vs-metric is a query-time
representation choice (`unit=EN` or `unit=SI`).

---

## 6. Naming conventions and the publisher zoo

### 6.1 Office hierarchy and the regional-rollup pattern

| Office code | Type | Notes |
|---|---|---|
| `NWD` | Division | Northwestern Division |
| `NWDM` | Division regional | Missouri River Region — publishes for NWO + NWK |
| `NWDP` | Division regional | Pacific Northwest Region — publishes for NWP + NWS + NWW |
| `NWO`, `NWK`, `NWS`, `NWP`, `NWW` | District | NW districts — **empty stubs in CDA** |
| `MVD` | Division | Mississippi Valley |
| `MVK`, `MVM`, `MVN`, `MVP`, `MVR`, `MVS` | District | MVD districts — publish their own |
| `SWD` | Division | Southwestern |
| `SWF`, `SWG`, `SWL`, `SWT` | District | SWD districts — publish their own |
| `LRD`, `LRDG`, `LRDO` | Division + regions | Great Lakes / Ohio River |
| `LRB`, `LRC`, `LRE`, `LRH`, `LRL`, `LRN`, `LRP` | District | LRD districts |

**Rule of thumb: in NW Division, query `NWDM` or `NWDP`, never NWO/
NWK/NWS/NWP/NWW.** Their district catalogs are essentially empty.

### 6.2 Sub-location naming patterns

Parent locations often have many child sub-locations. Naming encodes
the role:

| Pattern | Meaning | Example |
|---|---|---|
| `<parent>-D<depth>m`, `<parent>-D<depth>,#ft` | Depth-indexed water-quality sensor | `GWLW_S1-D3,0ft`, `BECR-D042,5m` |
| `<parent>-U<n>` | Generator unit | `BON-U1`, `TDA-U19` |
| `<parent>-PH<n>` | Powerhouse | `BON-PH2` |
| `<parent>-SB<n>` | Spill bay | `BON-SB1` |
| `<parent>-Lock<n>` | Lock chamber | `LWSC-Lock01` |
| `<parent>-FishLadder` | Fish ladder | `LWSC-FishLadder` |
| `<parent>-OT`, `-DG`, `-Spillway` | Outlet works / diversion gate / spillway | `GCL-OT` |
| `<parent>_S<n>` | Station number within a project | `GWLW_S1` |
| `<parent>-bl_####`, `-ab_####`, `-####_to_####` | Elevation zone (below/above/range) | `FTPK-bl_7000`, `FTPK-ab_8500` |
| `<parent>DCP` | Data Collection Platform identifier | `FortPeckDCP` |
| `<parent><city>-<river>` | Stream-location with city/river qualifiers | `GLMO-Glasgow-Missouri` |

**Sub-locations are mostly catalog scaffolding.** Even a project with
1,700+ children (Fort Peck) sees the vast majority carry zero
timeseries. Exceptions: depth-tagged WQ sites under instrumented
stations, where publisher `IRIDIUM-*` posts real data.

### 6.3 Publisher zoo

Each publisher (the version segment of a ts_id) has characteristic
scope, cadence, and template. Observed publishers:

#### Regional operational teams

| Publisher | Scope | Cadence | Template (typical) |
|---|---|---|---|
| `Best-MRBWM` *(Missouri River Basin Water Management)* | Mainstem Missouri reservoirs (Fort Peck, Fort Randall, Big Bend, Gavins Point, Garrison, Oahe) | Hourly + ~daily | **10 params:** Elev, Stor, Flow-In, Flow-Out, Energy, Precip, Temp-Air, Conc-DissolvedOxygen |
| `Raw-A2W` | Same scope, higher-frequency flow companion | 1Hour | Flow-In, Flow-Out |
| `CBT-RAW`, `CBT-REV` *(Columbia Basin Team, inferred)* | Columbia/Snake hydropower (Chief Joseph; presumably Bonneville, Grand Coulee, Dalles parents not yet located) | Hourly + ~daily | **10–18 params:** adds Flow-Spill, Flow-Gen, Elev-Tailwater, %-Saturation-TDG, Temp-Water |
| `CENWP-CALC` *(Corps of Engineers Northwest Portland, inferred)* | NWDP Portland District projects (Cougar Reservoir, ...) | Mixed | **5 params:** Elev-Forebay, Elev-RuleCurve, Flow-In, Flow-Out, Stor |

#### District operational teams

| Publisher | Scope | Cadence | Template (typical) |
|---|---|---|---|
| `Ccp-Rev` *(Tulsa District, inferred)* | SWT flood-control reservoirs (Foss, ...) | 15-min through 1-month | **30+ params:** adds %-Conservation/Flood Pool Full, Stor-Conservation/Flood Pool, Volume-Res In/Out, Evap, Dir-Wind |
| `MVDhist-rev` *(Mississippi Valley Division historical)* | MVS named river gages | ~1Day | **1 param:** Stage |
| `Computed`, `MANUAL`, `Regi`, `REGI` | MVS lake projects (Carlyle Lk) | Daily | **~5 params:** Flow-Out, Precip, project status codes |
| `Rev-Regi-*`, `Rev-Ccp` | SWT companions to `Ccp-Rev` | Hourly to daily | Flow computations and adjustments |
| `Metvue-Computed` | Areal precipitation aggregations | Hourly to daily | Precip-Mean Areal |

#### Field sensor relays and derived

| Publisher | Used for |
|---|---|
| `IRIDIUM-RAW`, `IRIDIUM-REV` | Satellite-relayed water-quality sensor data on depth-tagged sub-locations |
| `NWSRADIO-RAW`, `NWSRADIO-REV` | NWS weather-radio-relayed sensor data, alternate path on the same sites |
| `MIXED-REV`, `MIXED-COMPUTED-REV` | Blended/merged series from multiple upstream sources |
| `Best-...` (general) | "Best estimate" merge of raw + revised + manual |
| `*-Computed` (general) | Derived/computed series — always paired with an upstream raw |

**Variance is high within and across offices.** Examples:
- `SWT/FOSS` carries 31 parameters; `SWT/CHOU-Lock` carries 0
- `MVS/Carlyle Lk` carries 5; `MVS/Mark Twain Lk` carries 0
- 20+ `LWSC-*` records exist at the Ballard Locks site, 0 publish;
  the canal pool elevation is at `NWDP/LWD` under `CBT-RAW`
- 17+ `BECR-*` (Bear Creek Dam) variants across NWO/NWD/NWDM, none publish

A useful pattern when stuck: when name-matching fails or finds only
ghosts, **search by coordinates** for sibling records. Co-located
records under different ids often hold the operational data.

---

## 7. Using cwms-python

The official Python wrapper. `pip install cwms-python`. Repo:
https://github.com/HydrologicEngineeringCenter/cwms-python.

### 7.1 Setup

```python
import cwms

# Initialize a session (auth optional for read endpoints)
cwms.init_session(
    api_root="https://cwms-data.usace.army.mil/cwms-data/",
    # api_key="...",  # required for write endpoints
    # token="...",     # alternative: OIDC bearer token
    pool_connections=100,  # default; increase for high-concurrency callers
)
```

Read endpoints work without auth.

### 7.2 Architecture

- **Sync** (requests + requests-toolbelt). Not async. For async
  callers, wrap in `asyncio.to_thread(...)`.
- **Connection pooling and retry built in.** Exponential backoff,
  6 attempts on 403/429/502/503/504.
- **Returns** raw JSON (and, in `timeseries`/some modules, a `Data`
  wrapper that exposes both `.json` and `.df` for pandas).
- **Pandas is a required dependency** (~50MB install). You can avoid
  invoking pandas conversions by reading `.json` rather than `.df`.

### 7.3 Module layout

Each subpackage maps to a CDA resource family:

| Module | What it wraps |
|---|---|
| `cwms.api` | Low-level HTTP (`get`, `post`, `patch`, `delete`, `get_with_paging`) |
| `cwms.catalog` (`.catalog`, `.blobs`, `.clobs`) | Paginated catalog search; blobs/clobs |
| `cwms.locations` | Location CRUD |
| `cwms.projects` | Project metadata |
| `cwms.timeseries` (`.timeseries`, `.timeseries_bin`, `.timeseries_group`, `.timeseries_identifier`, `.timeseries_profile`, `.timeseries_profile_instance`, `.timeseries_profile_parser`, `.timeseries_txt`) | All timeseries variants |
| `cwms.levels` (`.location_levels`, `.specified_levels`) | Location levels + vocabulary |
| `cwms.forecast` (`.forecast_spec`, `.forecast_instance`) | Forecasts |
| `cwms.ratings`, `cwms.measurements`, `cwms.outlets`, `cwms.turbines`, `cwms.standard_text`, `cwms.users` | Specialized resource families |

### 7.4 Representative function signatures

(Not exhaustive — read the source for the full list.)

```python
# Locations / projects
cwms.locations.get_location(office_id, location_id, unit="EN")
cwms.locations.get_locations(office_id=None, location_ids=None, units="EN", ...)
cwms.projects.get_project(office_id, project_id)

# Timeseries
cwms.timeseries.get_timeseries(
    ts_id, office_id, unit="EN", begin=None, end=None, ...
) -> Data
cwms.timeseries.get_multi_timeseries_df(
    ts_ids, office_id, unit="EN", begin=None, end=None, max_workers=30
) -> pandas.DataFrame  # parallel fetch, thread pool

# Levels
cwms.levels.get_location_levels(
    level_id_mask=None, office_id=None, unit=None, datum=None,
    begin=None, end=None, page=None, page_size=None
) -> Data
cwms.levels.get_location_level(
    level_id, office_id, effective_date, unit=None
) -> Data
cwms.levels.get_level_as_timeseries(
    location_level_id, office_id, unit, begin=None, end=None, interval=None
) -> Data  # the keystone for value-with-context — but see §8

# Forecasts
cwms.forecast.get_forecast_spec(spec_id, office_id, designator=None)
cwms.forecast.get_forecast_instances(office_id, name=None, ...)
```

### 7.5 Pagination

`api.get_with_paging(selector, endpoint, params, ...)` handles cursor
pagination transparently — it follows `nextPage` cursors until
exhausted. Most consumer functions in higher modules invoke it
internally.

---

## 8. Gotchas

A consolidated reference. Each item is something that will burn time
if you don't know about it.

### Data-model gotchas

| | |
|---|---|
| NW district stubs | `office_id` of NWO/NWK/NWS/NWP/NWW returns near-empty catalogs. Use NWDM/NWDP instead. |
| Ghost records | Many catalog rows have zero timeseries. Cheapest detection: `list_parameters(loc)` returns empty. ~50%+ ghost rate is normal. |
| Co-located variants | A single facility may have 5–20 catalog ids; usually only 1–2 publish. Search by coordinates when name matching finds ghosts. |
| Sub-locations rarely publish | Depth-tagged and unit-tagged children of major projects (FTPK1, BON-U1, BECR-D042,5m) are mostly empty. Operational data sits on the parent PROJECT. |
| Publisher tag is the truth | Always inspect the ts_id version segment to know who produced the data. Prefer `*-REV` over `*-RAW`; prefer `Best-*` when present. |
| Composite key for levels | A level needs `(level_id, office, effective_date)` for unique retrieval. Newer effective_dates supersede older. |
| Naming convention parsing | Split parameter codes on the *first* hyphen for base+sub. Don't split `Stor-Conservation Pool` into 3 pieces. |
| Published vs computed coordinates | Both can be present on a Location; use `publishedLatitude/Longitude` for citation. |
| Datums are required | Lat/lon without `horizontalDatum` is ambiguous at meter scale. Preserve it. |

### API / wrapper gotchas

| | |
|---|---|
| `cwms-python` is sync | Use `asyncio.to_thread()` from async code. The wrapper uses thread pools internally for multi-fetch (`max_workers=30`). |
| Pandas is a required dependency | ~50MB install. Use `.json` accessor to avoid the conversion path. |
| `get_level_as_timeseries` broken for seasonal levels | [Open issue #286, March 2026](https://github.com/HydrologicEngineeringCenter/cwms-python/issues/286). This is the keystone endpoint for value-with-context. Plan to either patch around it or hit CDA directly until upstream fixes. |
| Error suppression in some write paths | Issues #255, #277, #287 note silent failures on api-down / multithreaded store / store chunk errors. Read paths are mostly fine; verify on writes. |
| `get_project` returns format errors for some PROJECTs | Observed on `NWDM/FTPK`: `"Formatting error: No Format for this content-type and data-type (application/json;version=2, cwms.cda.data.dto.project.Project)"`. May affect other projects. Workaround: fetch the underlying Location instead. |
| `forecast-instance` "requires an id" | `office + location_id` isn't enough; supply `name` (spec id) or filter by `designator` + dates. |
| `kind=PROJECT` filter is exact | If a location has null `kind`, it won't match. NWO records are all `kind=SITE` even when they represent dams. |
| Match-everything regexes rejected | The catalog endpoint refuses `.`, `.*`, `.+`. Use a single common letter as a permissive probe. |
| Cursors are session-bound | If the catalog snapshot shifts mid-pagination, the cursor returns `NOT_FOUND`. Re-issue the query without the cursor. |
| Forecast value location | `spec.timeSeriesIds` is a *list* of ts_ids holding the forecast values. The instance may also carry a binary `fileData` attachment. Either or both. |
| Forecast `name` ambiguity | The `name` query param means *spec id* in some endpoints, *instance id* in others. Always include `office` + at least one disambiguator. |

### Documentation gotchas

| | |
|---|---|
| Read-the-Docs deep links 404 | The CDA readthedocs index works, but several sub-pages return 404. Fall back to the Java DTOs and the Python wrapper source. |
| Swagger UI is partial | Lists endpoints but field-level schemas are sometimes thin. Same fallback. |
| Java DTOs are canonical | When in doubt, read `cwms-data-api/src/main/java/cwms/cda/data/dto/`. Field names, types, and Javadoc/`@Schema` annotations are the ground truth. |

---

## 9. Common questions and how to approach them

These are illustrative — not meant to be prescriptive. They show how
the entities and conventions above combine to answer realistic
questions.

### 9.1 "What's the current value of X at named place Y?"

Conceptual queries:
1. **Resolve name → location(s).** Catalog search by Y across name
   fields. Often returns multiple candidates.
2. **Confirm the right place.** Compare coordinates (or description)
   against user intent.
3. **Find the publisher for X at this location.** Enumerate
   timeseries; pick the one matching parameter X with the most
   trusted publisher (`Best-*` over `Raw-*`, `*-REV` over `*-RAW`).
4. **Fetch the latest value.** Read the most recent point.
5. *(Ideally)* Pull associated location levels and add context.

Naïve round-trips: 4–5. With a well-designed tool: 1.

### 9.2 "What's the trend / history?"

1. Resolve and select the canonical ts_id (as in 9.1, steps 1–3).
2. Fetch a windowed series with appropriate downsampling for the
   time horizon.
3. Optionally fetch levels valid over the same window (for plotting a
   threshold line).

### 9.3 "Is the lake at concerning levels?"

Using the `/levels/{id}/timeseries` shortcut so seasonal / timeseries /
virtual levels don't need client-side branching:

1. Resolve name → location.
2. Fetch current value of relevant parameter (`Elev` or `Stor`) over
   a small window around now.
3. List location levels matching `parameter=<P>` and effective at now
   — filter by `level-id-mask=<location>.<P>.*`.
4. For each level, fetch its value at now via
   `/levels/{level_id}/timeseries?begin=now&end=now&interval=1Hour`
   (normalizes all four varieties into a single shape).
5. Compare current observation to each threshold; classify status
   (e.g. "above top of conservation pool", "below action stage").

*Caveat:* see §8 — the wrapper's `get_level_as_timeseries` has a
known bug for seasonal levels. Hit CDA directly for that endpoint
until fixed.

### 9.4 "What's the forecast?"

1. Resolve name → location.
2. List forecast specs at the location: `/forecast-spec?office=...&location-id=...`.
3. For each spec, fetch the latest instance with the most recent
   `issueDateTime`.
4. Read predicted values:
   - If `spec.timeSeriesIds` is populated, fetch those ts windowed to
     the instance's `firstDateTime`–`lastDateTime`.
   - If `fileData`/`fileDataUrl` is set, download and parse per
     `fileMediaType`.

### 9.5 "Compare two locations"

Run steps 9.1.1–9.1.4 twice in parallel (`cwms.timeseries.get_multi_timeseries_df`
handles parallel fetch). Normalize units; align timestamps if cadences
differ.

### 9.6 "What data exists at this place?"

1. Resolve name → location, including co-located variants (records
   within ~100m of the same coordinates).
2. For each, enumerate publishers and parameter sets.
3. Surface the union, grouped by publisher.

### 9.7 "What's available in this region?"

1. Filter the catalog by office or geographic bounding box (CDA
   stores `latitude`/`longitude` per record; filter client-side).
2. Drop ghosts (records with 0 timeseries).
3. Group by publisher and parameter to communicate coverage.

### 9.8 "Which publishers report on parameter X?"

CDA doesn't have a parameter→publishers reverse index. The
workaround: enumerate the catalog, list ts at each location, group
ts_ids by version segment, filter to those with parameter X. This is
expensive — a tool serving this question should cache the
parameter→publisher map.

### 9.9 "Tell me about this dam"

1. Resolve name → Location.
2. Fetch the Project record at the same `(office, name)` (read
   `cwms.projects.get_project`).
3. Surface `authorizingLaw`, `projectOwner`, hydropower/sedimentation
   descriptions, federal/non-federal costs, etc. The Location alone
   doesn't carry this.

---

## 10. Tool-design implications

If you're building a tool layer on top of CDA, the patterns below pay
back the most.

### 10.1 Use cwms-python as transport; build value-add on top

Don't reinvent HTTP, retry, content negotiation, or DTO
deserialization. cwms-python handles all that. The Python wrapper is
sync; wrap calls in `asyncio.to_thread()` for async tool runtimes.
For the handful of endpoints with known wrapper bugs (level-as-
timeseries seasonal, currently), fall back to direct httpx calls.

### 10.2 Catalog enrichment is the highest-leverage addition

The default catalog search returns identifiers without data-bearing
hints. Enrich each match with:

- `parameter_count` — immediate ghost detection
- `publishers: [...]` — fingerprint of who publishes here
- `last_data_timestamp` — freshness signal
- `co_located: [...]` — sibling records at the same coordinates

These four fields turn "search returns 20 hits, agent calls
list_parameters on each" into "search returns 20 hits and agent
immediately knows which carry data."

### 10.3 Value-with-context fetches

The single most useful tool shape is "current value + applicable
thresholds + classification." The `/levels/{id}/timeseries` endpoint
already does the heavy lifting of normalizing level varieties; what's
missing is a tool that:

1. Lists location levels matching the parameter at the location
2. Converts each to a timeseries over the request window
3. Fetches the observation timeseries over the same window
4. Returns a joined response with observation, threshold bands, and a
   classification label

### 10.4 Publisher-aware search

Surface publishers as a first-class concept:
- "List publishers active in office X / at location L / for parameter P"
- "Resolve name to data-bearing locations only (filter ghosts)"
- "Pick the canonical (most-trusted) ts_id for parameter P at location L"

### 10.5 Geographic / spatial search

CDA carries coordinates per record but doesn't expose bbox or
radius search. Build it client-side: cache the catalog, filter on
`(latitude, longitude)`, dedupe co-located records.

### 10.6 Bulk and multi-entity operations

Common analytical questions involve >1 location. `cwms.timeseries.get_multi_timeseries_df`
already parallelizes — expose this as your default for "values across
a basin" or "compare reservoirs."

### 10.7 Cache strategy

- **Office and parameter catalogs** rarely change: long TTL (hours/days)
- **Location catalog** changes infrequently: medium TTL (hour scale)
- **Timeseries values** change constantly: short TTL or pass-through
- **Levels** change rarely but versioned: cache by `(level_id, effective_date)`
- **Publisher registry** (derived): rebuild daily

### 10.8 Error envelopes

Wrap CDA errors in a uniform envelope with at least:

```
{ok: bool,
 data: <payload> | null,
 error: {code, message, hint, retry_after_ms, temporary} | null,
 source: {endpoints_called, api_root, ...},
 request_id}
```

Maps cleanly onto the cwms-python exception hierarchy (`ApiError`,
`NotFoundError`, `PermissionError`) plus the underlying HTTP status.

---

## 11. Reference — where to look for specific things

| If you need... | Look at... |
|---|---|
| Canonical field schema for an entity | `cwms-data-api/src/main/java/cwms/cda/data/dto/<entity>.java` |
| The list of CDA endpoints + params | Swagger UI, the Java `*Controller.java` files, or the `cwms-python` module that wraps it |
| Python invocation patterns | `cwms/<resource>/<resource>.py` source; example notebooks under `cwms-python/examples/` |
| What a publisher tag means | This document §6.3 (empirical zoo) — there is no canonical publisher registry |
| Office hierarchy / regional rollup | This document §6.1 — derived empirically; reflects USACE operational organization |
| Sub-location naming convention | This document §6.2 — empirical |
| Parameter vocabulary | This document §5; canonical via `Parameter` DTO + `GET /parameters` |
| Whether a specific endpoint works | Try it. The wrapper and Swagger have minor drift. |

---

## 12. Open empirical questions

What this document can't yet tell you because it wasn't sampled
through to a confident answer:

- **Forecast publisher conventions.** The forecast entity schema is
  known (§4.5), but the empirical question of which `sourceEntityId`
  values each office uses, what version-segment naming the forecast
  ts_ids adopt, and whether file-attachment or linked-ts_ids
  dominates for which kinds of forecasts is unknown. Worth a focused
  probe of `/forecast-spec` against representative offices.
- **Pacific NW major hydropower parent records.** Bonneville, Grand
  Coulee, Dalles — sub-locations and unit records are observable
  (`BON-U1`, `GCL-Original`, `TDA-U19`), but whether PROJECT-kind
  parent records exist for these (analogous to `CHJ`) wasn't
  confirmed. Unknown whether data for these projects lives on a
  parent or only across the unit sub-locations.
- **Coverage of MVD / LRD / NAD / SAD / SPD / POD divisions** beyond
  the MVS / SWT spot checks. District publishing patterns there may
  diverge from what's documented here.
- **Whether non-NW water-quality projects** use the same depth-sub-
  location pattern as the NW Lake Washington sites.
- **Cross-office facility relationships.** When the same physical
  facility appears under multiple `office_id`s (BECR appears in NWO,
  NWD, NWDM), is there metadata declaring them as the same thing, or
  is coordinate clustering the only signal?
- **Update cadence and historical depth.** How far back does each
  publisher hold data? How quickly does `*-RAW` propagate to `*-REV`?
  How often is `Best-*` recomputed?
- **Empirical level conventions.** Which `specified_level_id`
  vocabulary terms does each office actually use (is "Top of
  Conservation Pool" the literal id or a long-name)? How common are
  seasonal vs timeseries vs virtual levels in production? How often
  do effective dates rotate?

Each is a focused probe — a small number of CDA calls per item.

---

## 13. Sources

### Canonical CDA DTOs (USACE/cwms-data-api, `develop` branch)

- [`Office.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/Office.java)
- [`Location.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/Location.java)
- [`Parameter.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/Parameter.java)
- [`TimeSeriesIdentifierDescriptor.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/TimeSeriesIdentifierDescriptor.java)
- [`Catalog.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/Catalog.java)
- [`locationlevel/LocationLevel.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/locationlevel/LocationLevel.java) and `SeasonalLocationLevel.java`, `TimeSeriesLocationLevel.java`, `VirtualLocationLevel.java`, `SeasonalValueBean.java`
- [`forecast/ForecastSpec.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/forecast/ForecastSpec.java)
- [`forecast/ForecastInstance.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/forecast/ForecastInstance.java)
- [`project/Project.java`](https://github.com/USACE/cwms-data-api/blob/develop/cwms-data-api/src/main/java/cwms/cda/data/dto/project/Project.java)
- DTO directory: [`dto/`](https://github.com/USACE/cwms-data-api/tree/develop/cwms-data-api/src/main/java/cwms/cda/data/dto)

### Python wrapper (HydrologicEngineeringCenter/cwms-python)

- Repository: [cwms-python](https://github.com/HydrologicEngineeringCenter/cwms-python)
- Latest release: v1.0.7 (2026-03-31)
- Key modules:
  [`cwms/api.py`](https://github.com/HydrologicEngineeringCenter/cwms-python/blob/main/cwms/api.py) (HTTP core),
  [`cwms/timeseries/timeseries.py`](https://github.com/HydrologicEngineeringCenter/cwms-python/blob/main/cwms/timeseries/timeseries.py),
  [`cwms/levels/location_levels.py`](https://github.com/HydrologicEngineeringCenter/cwms-python/blob/main/cwms/levels/location_levels.py),
  [`cwms/levels/specified_levels.py`](https://github.com/HydrologicEngineeringCenter/cwms-python/blob/main/cwms/levels/specified_levels.py)
- Examples: `cwms-python/examples/` (8 Jupyter notebooks)
- License: MIT

### CDA documentation

- [Live API root](https://cwms-data.usace.army.mil/cwms-data/)
- [Swagger UI](https://cwms-data.usace.army.mil/cwms-data/swagger-ui.html)
- [Read the Docs](https://cwms-data-api.readthedocs.io/) — narrative docs (some deep pages 404)
- [USACE/cwms-data-api](https://github.com/USACE/cwms-data-api) — Java source
