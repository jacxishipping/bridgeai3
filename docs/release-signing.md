# Signed Fleet Releases (Ed25519)

Release workflow [`.github/workflows/release.yml`](../.github/workflows/release.yml) produces `release-manifest.json` on every `v*` tag. When the repository secret **`AI_BRIDGE_RELEASE_SIGNING_KEY`** is set to a PKCS8 Ed25519 private PEM, the job also writes **`release-manifest.json.sig`**, which unlocks:

- Agent verify / apply (`--check-update` / `--apply-update`)
- Control-plane 1-click `POST /api/devices/{id}/update`
- Staged rollout rings (`canary` → `early` → `general`) on hosts that trust the matching public key

## Generate a production keypair (once)

```bash
# Writes keys/private.pem (0600) and keys/public.pem — never commit private.pem
python -m ai_bridge.updates keygen --out-dir keys/release
```

Or use the helper:

```bash
python scripts/generate_release_signing_key.py --out-dir keys/release
```

## Configure GitHub + fleet

1. Copy the **entire** `private.pem` PEM into the Actions secret `AI_BRIDGE_RELEASE_SIGNING_KEY` (raw PEM text, including `BEGIN`/`END` lines).
2. Distribute `public.pem` to agents via `AI_BRIDGE_UPDATE_PUBLIC_KEY` / `--public-key` (or bake into installer config).
3. Retag / re-run the Release workflow so artifacts include `release-manifest.json.sig`.
4. Dry-run locally anytime:

```bash
python scripts/release_manifest_dryrun.py --evidence-out .run/release_manifest_dryrun.json
```

## Rotate

Generate a new keypair, update the secret and agent public keys together, then cut a new signed `v*` release. Agents refuse unsigned or wrong-key manifests (fail-closed).
