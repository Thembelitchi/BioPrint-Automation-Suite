# Expert Reviewer Guide

Thank you for looking at this. I built BioPrint Automation Suite to connect industrial automation, knowledge graphs and clinical-laboratory quality control, three fields that rarely share a codebase. That means no single reviewer covers all of it, and every part needs someone who knows that domain better than I do.

This guide gives each kind of reviewer a short path: what is built, where to look, a 10-minute exercise, the questions I most want answered, and the limitations I already know about.

> **Ground rules.** Everything is simulated, with no physical hardware. Every physical parameter is listed with its basis and confidence in the [assumptions register](model-assumptions.md). Where something is a placeholder, I say so. Please treat "this number is wrong, here is a better one" as the most useful feedback there is.

| You are a… | Start here | Time to first insight |
|---|---|---|
| [LFAM process engineer](#lfam-process-engineer) | BioPrint-PID config + layer-time chart | 10 min |
| [Polymer / materials scientist](#polymer--materials-scientist) | Melt model + assumptions A-08 to A-16 | 10 min |
| [Robotic construction operations / automation architect](#robotic-construction-operations--automation-architect) | Graph queries, IFC hooks, Guard scaling plan | 15 min |
| [Industrial digital twin architect](#industrial-digital-twin-architect) | Graph schema + standards alignment | 15 min |
| [Controls engineer](#controls-engineer) | PID / Westgard in Python and Structured Text | 15 min |

## 5-minute setup

```bash
git clone https://github.com/Thembelitchi/BioPrint-Automation-Suite.git
cd BioPrint-Automation-Suite/projects/02_bioprint_pid
pip install -r requirements-dev.txt
python src/app.py          # about 1 s; charts and data in output/
python -m pytest           # 70 tests
```

GraphPrint-Twin also needs Neo4j: `docker compose up -d neo4j` from the repo root, then `cd projects/01_graphprint_twin && pip install -r requirements-dev.txt && python src/main.py`.

**Try your own values without editing code.** Create a TOML file containing only what you want to change and run:

```bash
python src/app.py --config my_values.toml --report-only
```

`--report-only` writes all the charts and still prints which PRD checks pass, but always exits 0. That's useful when your values deliberately push the tuned loop out of spec. A worked example is in [`config/examples/winter_shop_high_fibre.toml`](../projects/02_bioprint_pid/config/examples/winter_shop_high_fibre.toml).

---

## LFAM process engineer

*Large-format additive manufacturing: gantry or robot-arm extrusion of fibre-reinforced polymers.*

**What's built**
- Barrel/nozzle thermal model with heater input, ambient loss and cold-feed load (`src/extruder_physics.py`)
- Gantry-coupled auger flow (slows at corners) and a cold-extrusion interlock
- Shear-thinning nozzle-pressure model, calibrated to 45 bar at 230 °C
- **Layer-time window vs factory ambient** (`src/layer_thermal.py` → `output/layer_time_window.png`): the earliest and latest time the next layer can be laid, from lumped bead cooling against a "support" and a "bond" temperature

**10-minute exercise.** Copy the `[thermal]`, `[gantry]` and `[layer]` sections of `config/default.toml`, put in your machine's values (heater power, throughput, bead size, cooling, your bond and support temperatures), and run with `--config`. Look at `layer_time_window.png` and `pid_step_response.png`.

**Questions I'd most like answered**
1. Is **45 bar at 230 °C and 18 kg/h** a plausible nozzle pressure for a 20 × 5 mm GF-PETG bead? What do you see?
2. What layer-time window do you work to for GF-PETG? Do you control on **time** or on **measured substrate temperature** (IR)? Are 90 °C (bond) and 120 °C (support) sensible thresholds? These are A-26, the weakest assumptions in the model.
3. Do you couple screw speed to gantry speed as modelled, or run closed-loop **pressure control**?
4. Is a 2 °C interlock band with no soak timer acceptable, or would you insist on a soak?
5. Which disturbances actually ruin prints in your hall: drafts, wet feed, bridging in the hopper, nozzle wear?

**Known limitations:** single lumped thermal zone (real barrels have 3–6 zones plus the nozzle), no inter-layer re-heating, no warping or residual-stress model, and a toolpath that is a periodic placeholder rather than real G-code.

## Polymer / materials scientist

*Rheology, recycling and composite behaviour of PETG / HDPE with structural fibres.*

**What's built**
- Power-law melt `ΔP ∝ K(T, composition)·Qⁿ` with n = 0.4 and Arrhenius K(T)
- **Fibre loading** via Krieger-Dougherty `η_r = (1 − φ/φ_m)^(−[η]φ_m)`, with wt % converted to volume fraction
- **Recycling** via chain scission: Mw × 0.95 per pass and `η ∝ Mw^3.4`
- `output/material_sensitivity.png`: pressure vs fibre loading for recycle generations 0–4, and pressure vs temperature
- In the knowledge graph, each `MaterialBatch` records `fibre_wt_pct`, `recycle_generation`, `moisture_ppm` and `material_definition`, and a query reports anomaly rate by recycle generation ([graph-schema.md](graph-schema.md#key-queries))

**10-minute exercise.** Read A-08 to A-16 in the [assumptions register](model-assumptions.md#composition-fibre-loading-recycling), then change `[material]` and `[rheology]` to values you trust and compare the sensitivity chart.

**Questions I'd most like answered**
1. What Krieger-Dougherty `[η]` and `φ_m` would you use for short glass fibre in PETG at typical aspect ratios? Is K-D the right family at all above ~20 wt %?
2. What Mw retention per reprocessing pass is realistic for **dried vs undried** rPETG? Should the model drive degradation from `moisture_ppm` (hydrolysis) instead of a fixed factor?
3. Is applying `Mw^3.4` to the power-law consistency K defensible, or would you move to Cross-WLF or Carreau-Yasuda?
4. Which batch properties should the graph capture so printability can be predicted before a print starts: MFI, intrinsic viscosity, fibre-length distribution, residual moisture?
5. How should fibre attrition across recycle passes be represented?

**Known limitations:** listed under the composition section of the [assumptions register](model-assumptions.md#composition-fibre-loading-recycling).

## Robotic construction operations / automation architect

*Directors and architects running modular-construction or prefab print factories.*

**What's built**
- A knowledge graph linking **site → hall → line → printer → component**, plus **wall → material batch** ([graph-schema.md](graph-schema.md))
- Each printed wall carries a valid **IFC GlobalId** and `IfcWall` class, so QC results can be joined to the BIM model ([standards-alignment.md](standards-alignment.md#ifc-iso-16739-printed-walls-in-bim)). A `Pset_BioPrintQuality` export is designed but not built.
- An operations query: "which walls were printed while a component had an open maintenance alert?", returned with IFC ids for a BIM viewer
- BioPrint-Guard (next release, v0.5.0): an async OPC UA predictive-maintenance gateway writing alerts into the same graph

**15-minute exercise.** Read the key queries in [graph-schema.md](graph-schema.md#key-queries) and the IFC section of [standards-alignment.md](standards-alignment.md). Then ask: would a project manager actually use this, and in which tool?

**Questions I'd most like answered**
1. What questions do your project managers ask after a failed or suspect print? I'd like the graph to answer them directly.
2. Are the proposed `Pset_BioPrintQuality` properties the right ones? Which BIM platform should the export target first?
3. For BioPrint-Guard: how many robots and signals per hall? Would you run one OPC UA server per robot, an aggregating server, or MQTT/Sparkplug B? (The v0.5.0 PRD includes a multi-robot scaling requirement.)
4. What alert lead time and false-alarm rate make predictive maintenance worth it against a 24-hour print window?
5. Where should this sit relative to your MES: feeding it, or reading from it?

**Known limitations:** single site, line and printer in the demo; no job orders or scheduling; IFC export not implemented; Guard not yet built.

## Industrial digital twin architect

*IT/OT bridge specialists: OPC UA information models, ISA-95, Asset Administration Shell.*

**What's built**
- Simulated 10 Hz PLC registers contextualised into a semantic graph at ingest, **bucketed per printed layer** so the graph stores context and summaries rather than raw series. Every bucket is attached to its printer and wall and chained to the previous layer (GraphPrint-Twin).
- **ISA-95** equipment hierarchy (`Site → Area → WorkCenter → WorkUnit`), an ISA-88 control module, and `MaterialBatch` as an ISA-95 Material Lot
- **AAS** identifiers (`aas_id`) on printers and components, with an IDTA submodel mapping
- Swappable `TelemetrySource` interface, so Modbus, OPC UA or Kafka can replace the simulator
- The full mapping and its gaps are in **[standards-alignment.md](standards-alignment.md)**

**15-minute exercise.** Compare [graph-schema.md](graph-schema.md) with your reference architecture and note what you'd restructure.

**Questions I'd most like answered**
1. Is the printer correctly a **Work Unit**, or would you model the printer as a Work Center with heads as units?
2. Telemetry is stored as **one `LayerTelemetry` bucket per printed layer** (stats always, raw arrays only for anomalous layers, implicit timestamps), not one node per sample: about 120 nodes per printer per day instead of ~864,000. Is the layer the right bucket, or would you use fixed time windows? At what scale would you move raw samples to a time-series store or historian and keep only a `raw_uri` pointer in the graph?
3. Which AAS submodels would you implement first, and would you stand up Eclipse BaSyx alongside Neo4j or generate AASX from the graph?
4. Should BioPrint-Guard's OPC UA model adopt **OPC 40001-1 Machinery** and **OPC 40010 Robotics** now, before more code depends on the custom namespace?
5. Would you put a **unified namespace** (MQTT + Sparkplug B) between OPC UA and the graph?

**Known limitations:** no Enterprise level, personnel model or job orders; AAS ids are placeholder URNs; no B2MML or AASX import/export.

## Controls engineer

*Automation and robotics controls: deterministic PLC code, loop tuning, saturation handling.*

**What's built**
- `DiscretePID` in Python: derivative on measurement, filtered D, clamped output, **conditional-integration anti-windup**
- A Westgard multirule monitor (the clinical-lab QC idea) on the barrel temperature
- **IEC 61131-3 Structured Text ports** of both blocks (`projects/02_bioprint_pid/plc/`), with 300 scans of reference vectors, TcUnit test suites, and a CI check that executes the ST logic and matches Python to 1e-9 ([PLC README](../projects/02_bioprint_pid/plc/README.md))
- A 50-seed robustness test on the closed loop, and an untuned-vs-tuned comparison showing windup to about 347 °C

**15-minute exercise.** Paste `plc/src/*.st` and `plc/tests/*.st` into a TwinCAT 3 or CODESYS project with TcUnit and run the suites. **This is the single most useful thing a controls engineer could do for the repo**, because the ST has been logic-verified but not compiled on a real toolchain.

**Questions I'd most like answered**
1. Does the ST compile and pass TcUnit on your toolchain as-is? Anything non-portable?
2. Conditional integration vs **back-calculation** anti-windup for a heater zone: which do you prefer, and why?
3. Should the Westgard monitor run every scan (current: ~10 % chance of one false reject in 3 s) or on a slower QC task, the way a lab runs controls per batch?
4. Would you add **feedforward from auger speed** to cancel the feed-cooling load the interlock introduces?
5. Is LREAL acceptable on your targets, or do you need REAL (and a looser tolerance on the vectors)?

**Known limitations:** single-loop SISO control; no cascade (melt-pressure → screw speed); gains tuned to a placeholder plant (A-27); no task-jitter or I/O-latency model.

---

## Glossary for cross-disciplinary readers

| Term | Plain meaning |
|---|---|
| **Anti-windup** | Stops a controller's integral term from building up while the output is already at 100 %, which would otherwise cause a large overshoot later |
| **Westgard rules** (1-2s, 1-3s, 2-2s) | Clinical-lab QC rules that judge a result by how many standard deviations it sits from target, and whether consecutive results agree |
| **Levey-Jennings chart** | A control chart with ±1, 2 and 3 SD lines, used daily in clinical labs |
| **Shear-thinning / power law** | The melt gets runnier the faster it is pushed; pressure grows with flowⁿ where n < 1 |
| **Krieger-Dougherty** | Equation for how solid particles or fibres raise a fluid's viscosity as their volume fraction approaches a packing limit |
| **Layer time** | Time before the nozzle returns to deposit on top of the same spot |
| **ISA-95** | International standard for how factory systems describe equipment, materials and production between the shop floor and business systems |
| **AAS (Asset Administration Shell)** | Industrie 4.0 standard "digital passport" for a physical asset, made of standard submodels |
| **IFC GlobalId** | The 22-character unique id of an element in an open-standard BIM model |
| **OPC UA** | The standard industrial protocol for reading machine data with a typed information model |
| **Structured Text (IEC 61131-3)** | A Pascal-like language that PLCs run deterministically on a fixed cycle |

## How to give feedback

- **Issue form:** [Expert review](https://github.com/Thembelitchi/BioPrint-Automation-Suite/issues/new?template=expert_review.yml). Choose your area, cite the assumption ID if there is one, and give your value and its basis.
- **Pull request:** a TOML override in `config/examples/` with a comment explaining the source is the easiest contribution to merge, and CI will run it automatically.
- **Direct:** [LinkedIn](https://www.linkedin.com/in/thembelihle-gumede-29a68095). I'm happy to set up a short call to walk through the repo.

Contributors who change a model value or a design decision are credited in the CHANGELOG and the release notes.
