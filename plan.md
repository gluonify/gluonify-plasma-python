# plan.md: what is left to do (gluonify-plasma-python)

1. **Publication** (user): environment `pypi` on GitHub, pending trusted publishers for `gluonify` and `gluonify-fastapi` on PyPI, reservation of derived names; first tag `v0.1.0` (README § 11).
2. **Bup reads the Python markers** (`gluonify-bup`, framework `python`, python.md V3-2): `site-packages/*.dist-info/entry_points.txt`, section `[gluonify.uses]`, trusted names `gluonify`/`gluonify-*`, merge as `?name` (README § 8; reference reader `gluonify.markers.infer`).
3. **gDown over Bolt on the platform**: Higgs starts gDown with `GLUONIFY_GDOWN_BOLT_ENABLED=false`; enable Bolt (7687, TLS, network rule for `uses: gdown`) and inject `GLUONIFY_GDOWN_BOLT_URL`, or add an HTTP client (`POST /db/<base>/query`) to the SDK as a fallback. Decision for the user.
4. **`gdownDatabase` inferred** for Python (Java infers it from configuration references): a second entry point group, or a key in `[tool.gluonify]`.
5. **Developer machine mode** (`~/.gluonify/config.json`, discovery document, developer token refresh), as `DeveloperConfig`/`DeveloperTokens` in Java.
6. **Clients** for Crystallon (rendering), Fermion and Photon (signature check of webhooks), each with its marker.
7. **e2e** on a real cluster with gluonify-source-python (V3-4, calqued on `e2e-source.sh`).
8. Pin the GitHub Actions by commit SHA.
9. Least privilege: move gDown into `gluonify-gdown` if inferring `?gdown` for every app using `gluonify` is not acceptable.
