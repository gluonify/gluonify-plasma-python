"""The uses markers: the contract Bup reads (README, "The uses markers")."""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

from gluonify import markers

ROOT = Path(__file__).resolve().parents[1]


def write_dist(site: Path, name: str, entry_points: str | None, folder: str | None = None) -> None:
    d = site / f"{folder or name.replace('-', '_')}-0.1.0.dist-info"
    d.mkdir(parents=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: 0.1.0\n\nName: not-a-header\n")
    if entry_points is not None:
        (d / "entry_points.txt").write_text(entry_points)


def test_each_distribution_declares_the_services_it_reaches() -> None:
    data = tomllib.loads((ROOT / "packages/gluonify/pyproject.toml").read_text())
    assert data["project"]["entry-points"]["gluonify.uses"] == {"charm": "gluonify.charm"}
    # The fastapi extra adds dependencies, never a marker; there is no other distribution.
    assert set(data["project"]["optional-dependencies"]) == {"fastapi"}
    assert not (ROOT / "packages/gluonify-fastapi").exists()


def test_installed_metadata_carries_the_markers() -> None:
    eps = {(ep.name, ep.value, ep.dist.name if ep.dist else None) for ep in metadata.entry_points(group="gluonify.uses")}
    assert ("charm", "gluonify.charm", "gluonify") in eps
    assert not any(name == "gdown" for name, _, _ in eps)


def test_infer_reads_trusted_distributions_only(tmp_path: Path) -> None:
    write_dist(
        tmp_path, "gluonify", "[gluonify.uses]\ncharm = gluonify.charm\ngdown = gluonify.gdown\n\n[console_scripts]\nx = y:z\n"
    )
    write_dist(tmp_path, "Gluonify_Crystallon", "[gluonify.uses]\ncrystallon = gluonify_crystallon\n", "gluonify_crystallon")
    write_dist(tmp_path, "evil", "[gluonify.uses]\nhiggs = evil\nbank = evil\n")
    write_dist(tmp_path, "gluonifyx", "[gluonify.uses]\nx = y\n")
    write_dist(tmp_path, "gluonify-bad", "[gluonify.uses]\nhiggs = a\nBad_Name = b\n")
    write_dist(tmp_path, "gluonify-noentry", None)
    write_dist(tmp_path, "gluonify-other", "[console_scripts]\na = b:c\n")
    write_dist(tmp_path, "gluonify-broken", "not an ini file [\n")
    (tmp_path / "nometa-1.dist-info").mkdir()
    r = markers.infer(tmp_path)
    assert r.services == ("charm", "crystallon", "gdown")
    assert r.sources == {"charm": ("gluonify",), "crystallon": ("gluonify-crystallon",), "gdown": ("gluonify",)}
    reasons = {(d, e, why) for d, e, why in r.ignored}
    assert ("evil", "higgs", "untrusted distribution") in reasons
    assert ("gluonifyx", "x", "untrusted distribution") in reasons
    assert ("gluonify-bad", "higgs", "invalid service name") in reasons
    assert ("gluonify-bad", "Bad_Name", "invalid service name") in reasons
    assert ("gluonify-broken", "", "unreadable entry_points.txt") in reasons


def test_neo4j_and_neomodel_infer_gdown(tmp_path: Path) -> None:
    write_dist(tmp_path, "neo4j", None)
    write_dist(tmp_path, "Neo4j", None, "neo4j_dup")
    write_dist(tmp_path, "neomodel", "[console_scripts]\nneomodel_install_labels = x:y\n")
    write_dist(tmp_path, "neo4j-rust-ext", None)  # not the driver itself: no inference
    r = markers.infer(tmp_path)
    assert r.services == ("gdown",)
    assert r.sources == {"gdown": ("neo4j", "neomodel")}
    assert r.uses(["charm"]) == ["charm", "?gdown"]
    assert markers.CLIENTS == {"neo4j": "gdown", "neomodel": "gdown"}


def test_merge_like_bup() -> None:
    r = markers.Inference(("charm", "gdown"), {}, ())
    assert r.uses(["charm", "photon"]) == ["charm", "photon", "?gdown"]
    assert r.uses(["?gdown"]) == ["?gdown", "?charm"]
    assert r.uses(None) == ["?charm", "?gdown"]


def test_normalize_and_trust() -> None:
    assert markers.normalize("Gluonify_FastAPI") == "gluonify-fastapi"
    assert markers.is_trusted("gluonify")
    assert markers.is_trusted("gluonify.fastapi")
    assert not markers.is_trusted("gluonifyfoo")
    assert markers.is_trusted("acme-x", ("acme",))


def test_the_venv_of_the_workspace() -> None:
    import gluonify

    site = Path(gluonify.__file__).resolve().parents[0]
    # editable installs keep the dist-info in site-packages of the venv
    venv_site = next(p for p in (ROOT / ".venv/lib").glob("python3.*/site-packages"))
    r = markers.infer(venv_site)
    assert r.services == ("charm",)
    assert r.sources == {"charm": ("gluonify",)}  # neo4j is not a dependency of the SDK
    assert site.exists()
