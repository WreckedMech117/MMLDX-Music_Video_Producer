# Reference API Exports

These files are immutable application-side copies of user-exported ComfyUI API graphs. The creator originals remain under `J:\Hermes-Remote\comfyui\workflowsbackup\API-Workflows`.

They are **reference evidence**, not graphs submitted directly by Music Video Producer. Some contain creator-specific media paths, multiple active output branches, or incomplete virtual/shared loader wiring. Runtime adapters in `src/music_video_producer/workflows.py` expose only reviewed semantic controls.

| File | SHA-256 | Audit status |
|---|---|---|
| `flux-user-export.json` | `0ed08fd477785d1e6661b4ca345a294cddb53074229521483f94837750c70f01` | 15 nodes; complete; matches Flux adapter with optional LoRA loader deliberately omitted |
| `h3-director-user-export.json` | `fd270d7f71744f39115881dfb2693b1f0c7bd5f424cc0dfd34f5703f42d023ed` | 12 nodes; API format, but required CLIP/video-VAE/audio-VAE links were omitted by virtual editor wiring; corrected by explicit runtime adapter |
| `h3-ltx23-user-export.json` | `d4027e76b2d1d3a7bd070505f2f493e275bcd519f9f6a5d0c692004e70890eca` | 56 nodes; all server node classes registered; combined creator path retained for reference only |
| `h3-ltx25-user-export.json` | `fe009123bf7e8c85841a8263cae7f85551ba44423b57423b309b8f6aa93909b4` | 65 nodes; all server node classes registered; contains hard-coded media and the diagnosed SeedVR2→LTX dimension boundary |
| `h3-ultra-references-user-export.json` | `bbcd1bcd1df6f7d826ccf439ce01b8817a517c171a2a72b0e83c1144e196ff46` | 29 nodes; all classes registered; media loader supports 9 pictures, 3 videos with paired audio, and 3 standalone audios; adapted as an explicit 18-node Ultra reference stage |

Imported: 2026-08-16.
