import io
import json
import csv
import codecs
import numpy as np
import numpy.typing as npt
import pandas as pd
import logging

from findpeaks import findpeaks
from typing import TypedDict, Any
from requests import Response, get
from collections.abc import Callable

from app.config.settings import settings
from celery import shared_task
from ..tasks import communication

logger = logging.getLogger(__name__)

URL = str


class Spectrum(TypedDict):
    file_url: str
    filename: str
    id: int
    sample: dict[int, str]
    format: str
    status: str
    category: str
    range: str
    metadata: str | dict | None


def validate_json(json_data) -> bool:
    if isinstance(json_data, dict):
        return True
    try:
        json.loads(str(json_data))
    except (ValueError, TypeError):
        return False
    return True


def download_file(url: URL) -> io.BytesIO | None:
    """
    Download file from given url and return it as an in-memory buffer
    """
    try:
        response: Response = get(url)
    except Exception as e:
        logger.error(e)
        return None
    file: io.BytesIO = io.BytesIO(response.content)
    file.seek(0)
    return file


def validate_csv(file: io.BytesIO, filename: str) -> io.BytesIO | None:
    try:
        dialect = csv.Sniffer().sniff(file.read(1024).decode('utf-8'))
        file.seek(0)
        has_header: bool = csv.Sniffer().has_header(file.read(1024).decode('utf-8'))
        file.seek(0)
        if has_header:
            file.close()
            return None

        csv_data = csv.reader(codecs.iterdecode(file, 'utf-8'), dialect)

        sio: io.StringIO = io.StringIO()

        writer = csv.writer(sio, dialect='excel', delimiter=',')
        for row in csv_data:
            if row.count(',') + 1 > 2:
                sio.close()
                return None
            writer.writerow(row)

        sio.seek(0)
        bio: io.BytesIO = io.BytesIO(sio.read().encode('utf8'))

        sio.close()
        file.close()

        bio.name = f'{filename.rsplit(".", 2)[0]}.csv'
        bio.seek(0)

        return bio
    except Exception as e:
        logger.error(e)
        return None


def find_peaks(file: io.BytesIO) -> npt.NDArray | None:
    """
    Find peaks in second array of csv-like data and return as numpy array.

    Peaks are filtered by their rank and height returned by findpeaks.
    Return None if none were found
    """
    try:
        data = np.loadtxt(file, delimiter=",")[:, 1]
        data = data / np.max(data)
        fp = findpeaks(method='topology', lookahead=2, denoise="bilateral")
        if (result := fp.fit(data)) is not None:
            df: pd.DataFrame = result["df"]
        else:
            return None

        filtered_pos: npt.NDArray = df.query('peak == True & rank != 0 & rank <= 40 & y >= 0.005')[
            "x"].to_numpy()
        return filtered_pos
    except Exception as e:
        logger.error(e)
        return None


def convert_dpt(file: io.BytesIO, filename: str) -> io.BytesIO | None:
    """
    Convert FTIR .1.dpt and .0.dpt files to .csv
    """
    try:
        csv_data = csv.reader(codecs.iterdecode(file, 'utf-8'))

        sio: io.StringIO = io.StringIO()

        writer = csv.writer(sio, dialect='excel', delimiter=',')
        for row in csv_data:
            writer.writerow(row)

        sio.seek(0)
        bio: io.BytesIO = io.BytesIO(sio.read().encode('utf8'))

        sio.close()
        file.close()

        bio.name = f'{filename.rsplit(".", 2)[0]}.csv'
        bio.seek(0)

        return bio
    except Exception as e:
        logger.error(e)
        return None


def convert_dat(file: io.BytesIO, filename: str) -> io.BytesIO | None:
    """
    Convert Bruker's Tracer XRF .dat files to .csv
    """
    try:
        line_count: int = sum(1 for line in file.readlines() if line.rstrip())
        file.seek(0)

        x_range: list[int] = [0, 40]
        x_linspace = np.linspace(x_range[0], x_range[1], line_count-1)
        counts = []

        with file as f:
            # [float(s) for s in f.readline().split()]
            header = f.readline()
            for line in f:
                counts.append(float(line.strip()))

        output = np.vstack((x_linspace, np.array(counts))).T

        sio: io.StringIO = io.StringIO()
        csvWriter = csv.writer(sio, delimiter=',')
        csvWriter.writerows(output)

        sio.seek(0)

        bio: io.BytesIO = io.BytesIO(sio.read().encode('utf8'))

        sio.close()
        file.close()

        bio.name = f'{filename.rsplit(".", 2)[0]}.csv'
        bio.seek(0)

        return bio
    except Exception as e:
        logger.error(e)
        return None


def construct_metadata(init, peak_data: npt.NDArray) -> dict | None:
    """
    Construct JSON object from existing spectrum metadata and peak metadata from processing
    """
    peak_metadata: dict[str, list[dict[str, str]]] = {
        "peaks": [{"position": str(i)} for i in peak_data]}
    if isinstance(init, str):
        return {**json.loads(init), **peak_metadata}
    elif isinstance(init, dict):
        return {**init, **peak_metadata}
    else:
        return None


dispatch: dict[str, Callable] = {
    "dpt": convert_dpt,
    "csv": validate_csv,
    "dat": convert_dat
}


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 0},
             name='spectra:process_spectrum')
def process_spectrum(self, id: int) -> dict[str, str]:
    """
    Process passed spectrum based on its filetype

    Filetype is defined in spectrum["format"]. Supported filetypes: .dpt
    """
    if (raw_spectrum := communication.get_spectrum(id)) is None:
        communication.update_status(id, "error")
        return {"message": f"Error retrieving spectrum with {id}"}
    spectrum: Spectrum = json.loads(raw_spectrum)["spectrum"]

    file_url: URL = f'{settings.hsdb_url}{spectrum["file_url"]}'
    filename: str = spectrum["filename"]
    filetype: str = spectrum["format"]

    communication.update_status(id, "ongoing")

    if (file := download_file(file_url)) is None:
        communication.update_status(id, "error")
        return {"message": f"Error getting spectrum file from server"}

    if filetype not in dispatch:
        communication.update_status(id, "error")
        return {"message": f"Unsupported filetype for spectrum with id {id}"}
    if (processed_file := dispatch[filetype](file, filename)) is None:
        communication.update_status(id, "error")
        return {"message": f"Error coverting spectrum with id {id}"}
    peak_data = find_peaks(processed_file)

    processed_file.seek(0)

    file_patch_response: Response | None = \
        communication.patch_with_processed_file(
            id, processed_file)

    metadata_patch_response: Response | None = None
    if validate_json(spectrum["metadata"]) and peak_data is not None:
        if (metadata := construct_metadata(spectrum["metadata"], peak_data)) is not None:
            metadata_patch_response = communication.update_metadata(
                id, metadata)

    processed_file.close()

    if metadata_patch_response is None \
            or file_patch_response is None:
        communication.update_status(id, "error")

    communication.update_status(id, "successful")

    return {"message": f"Done processing spectrum with id {id}"}
