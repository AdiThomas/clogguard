"""Registry of the open datasets ClogGuard uses for building, pretraining and benchmarking.

Open-dataset results are never reported as real-printer performance. Every converted run carries
``data_source="open_dataset"`` and the citation below.

Checksums: ``RemoteFile.sha256`` is ``None`` until the first download. After a download,
``data/open/<name>/checksums.json`` holds the SHA-256 of every file; copy the values here so later
downloads are verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RemoteFile:
    """One downloadable file: a direct ``url``; ``sha256`` is filled after first download."""

    name: str
    url: str
    sha256: str | None = None


@dataclass(frozen=True)
class DatasetInfo:
    name: str
    title: str
    landing_url: str
    licence: str
    citation: str  # APA 7
    expected_files: tuple[str, ...]  # names or glob patterns of the files the loader reads
    notes: str
    doi: str = ""
    # Direct download links. Empty when the host has no stable direct links we know of; the
    # downloader then prints manual instructions with the landing URL.
    files: tuple[RemoteFile, ...] = ()
    # Zenodo record id: the downloader can list files and links from the Zenodo API.
    zenodo_record: str | None = None
    manual_steps: str = ""
    extra_urls: tuple[str, ...] = field(default_factory=tuple)

    @property
    def urls(self) -> tuple[str, ...]:
        return (self.landing_url, *self.extra_urls, *(f.url for f in self.files))


UFRGS_LABELS: dict[str, str] = {
    "healthy": "healthy printer, nominal settings",
    "temp_220": "nozzle temperature set to 220 C",
    "temp_230": "nozzle temperature set to 230 C",
    "loose_head": "loose print head",
    "nozzle_03": "nozzle restricted to 0.3 mm",
    "nozzle_02": "nozzle restricted to 0.2 mm",
}

REGISTRY: dict[str, DatasetInfo] = {
    "ufrgs_vibration": DatasetInfo(
        name="ufrgs_vibration",
        title="Vibration dataset of an Ender 3 V2 printing 3DBenchy under healthy and faulty "
        "conditions (ADXL345, 3200 Hz)",
        landing_url="https://zenodo.org/records/22716165",
        licence="CC BY 4.0",
        # ASSUMPTION: year of publication taken as 2025 and title paraphrased; confirm against the
        # Zenodo record's "Cite as" box after first download.
        citation=(
            "da Cunha, L. K. T., & Weber, T. O. (2025). Vibration dataset of a 3D printer under "
            "healthy and faulty conditions [Data set]. Universidade Federal do Rio Grande do Sul. "
            "Zenodo. https://zenodo.org/records/22716165"
        ),
        expected_files=("benchy_*.parquet",),
        notes=(
            "26 Parquet files (about 1.1 GB), 27 prints (benchy_11_12_* holds two). Creality "
            "Ender 3 V2, ADXL345 triaxial accelerometer on the hotend at 3200 Hz; a 200 Hz "
            "filtered version also exists. Labels from file names: "
            + ", ".join(UFRGS_LABELS)
            + ". Column names unknown: the loader detects time and x/y/z columns heuristically."
        ),
        zenodo_record="22716165",
    ),
    "tud_force": DatasetInfo(
        name="tud_force",
        title="Hotend force data synchronised with G-code under misconfigured print settings",
        landing_url="https://tudatalib.ulb.tu-darmstadt.de/handle/tudatalib/4596",
        licence="CC BY 4.0",
        doi="10.48328/tudatalib-1772",
        citation=(
            "Dietz, Wüst, Spiehl, Dörsam, & Blaeser. (2025). Hotend force data of a fused "
            "filament fabrication printer under misconfigured process parameters [Data set]. "
            "TU Darmstadt TUdatalib. https://doi.org/10.48328/tudatalib-1772"
        ),
        expected_files=("*.csv", "*.parquet", "*.h5", "*.hdf5"),
        notes=(
            "Hotend force synchronised with G-code under misconfigured extrusion multiplier, "
            "speed, nozzle temperature, line width and layer height; includes the Python "
            "acquisition code. File formats unknown: the loader reads CSV/Parquet (HDF5 if h5py "
            "is installed) and maps the force column to force_g."
        ),
        # ASSUMPTION: TUdatalib (DSpace) bitstream links are not known without access; manual.
        manual_steps=(
            "Open the landing page, download every data file (and the acquisition code), and "
            "place them in data/open/tud_force/. Then run `clogguard data download tud_force` "
            "again to record checksums and provenance."
        ),
        extra_urls=("https://doi.org/10.48328/tudatalib-1772",),
    ),
    "agh_anomaly": DatasetInfo(
        name="agh_anomaly",
        title="3D printing data with provoked malfunctions (AGH University of Krakow)",
        landing_url="https://github.com/joanna-/3D-Printing-Data",
        licence="CC BY 4.0",
        citation=(
            "Sendorek, J., Szydlo, T., Windak, M., & Brzoza-Woch, R. (2020). FDM 3D printing "
            "data with provoked malfunctions (arXiv:2004.08817). arXiv. "
            "https://arxiv.org/abs/2004.08817. Published in Computational Science - ICCS 2021, "
            "Springer."
        ),
        expected_files=("**/*.csv",),
        notes=(
            "Head accelerometer, power and temperatures recorded with provoked malfunctions. "
            "CSV. Column names and sample rates are detected by the loader."
        ),
        # ASSUMPTION: default branch is "master"; if the archive 404s, try "main".
        files=(
            RemoteFile(
                name="3D-Printing-Data-master.zip",
                url="https://github.com/joanna-/3D-Printing-Data/archive/refs/heads/master.zip",
            ),
        ),
    ),
    "caxton": DatasetInfo(
        name="caxton",
        title="CAXTON: labelled nozzle-camera images for extrusion error detection",
        landing_url=("https://www.repository.cam.ac.uk/items/6d77cd6d-8569-4bf4-9d5f-311ad2a49ac8"),
        # ASSUMPTION: licence not yet checked; read the repository record before any use.
        licence="To be checked on the repository record",
        citation=(
            'Brion, D. A. J., & Pattinson, S. W. (2022). Data set for "Generalisable 3D printing '
            'error detection and correction via multi-head neural networks" [Data set]. '
            "University of Cambridge Apollo repository. "
            "https://www.repository.cam.ac.uk/items/6d77cd6d-8569-4bf4-9d5f-311ad2a49ac8"
        ),
        expected_files=("*.csv",),
        notes=(
            "1,272,273 nozzle-camera images labelled with flow rate, lateral speed, Z offset and "
            "hotend temperature. ClogGuard reads only the label CSV and can select a subset by "
            "flow-rate class; images are not downloaded or converted."
        ),
        manual_steps=(
            "Download only the label CSV from the repository record into data/open/caxton/ "
            "(the image archives are very large and not needed). Check the licence first."
        ),
    ),
}


def get(name: str) -> DatasetInfo:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown dataset {name!r}; known: {sorted(REGISTRY)}") from exc
