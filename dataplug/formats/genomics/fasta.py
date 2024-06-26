from __future__ import annotations

import io
import logging
import math
import re
import shutil
import time
from typing import TYPE_CHECKING

import numpy as np

from ...entities import CloudDataFormat, CloudObjectSlice, PartitioningStrategy
from ...preprocessing.metadata import PreprocessingMetadata

if TYPE_CHECKING:
    from typing import List
    from ...cloudobject import CloudObject
    from botocore.response import StreamingBody

logger = logging.getLogger(__name__)


def preprocess_fasta(cloud_object: CloudObject, chunk_data: StreamingBody,
                     chunk_id: int, chunk_size: int, num_chunks: int):
    chunk_offset = chunk_id * chunk_size

    t0 = time.perf_counter()
    data = chunk_data.read()
    t1 = time.perf_counter()

    logger.info("Got chunk data in %.2f s", t1 - t0)

    t0 = time.perf_counter()
    # we use greedy regex so that match offsets also gets the \n character
    matches = list(re.finditer(rb">.+(\n)?", data))

    sequences = []
    for match in matches:
        start = chunk_offset + match.start()
        end = chunk_offset + match.end()
        # seq_id = match.group().decode("utf-8").split(" ")[0].replace(">", "")
        sequences.append((start, end))

    if matches and b"\n" not in matches[-1].group():
        # last match corresponds to a cut sequence identifier, as newline was not read
        offset = chunk_offset + matches[-1].start()
        # read split sequence id line
        with cloud_object.open("rb") as fasta_file:
            fasta_file.seek(offset)
            seq_id_line = fasta_file.readline()
            # get the current offset after reading line, it will be offset for the start of the sequence
            end = fasta_file.tell()
        # seq_id = seq_id_line.decode("utf-8").split(" ")[0].replace(">", "")
        sequences.pop()  # remove last split sequence id added previously
        sequences.append((offset, end))

    t1 = time.perf_counter()
    logger.info("Found %d sequences in %.2f s", len(sequences), t1 - t0)

    arr = np.array(sequences, dtype=np.uint32)
    arr_bytes = arr.tobytes()
    return PreprocessingMetadata(metadata=arr_bytes)


def merge_fasta_metadata(cloud_object: CloudObject, chunk_metadata: List[PreprocessingMetadata]):
    map_results = [np.frombuffer(meta.metadata, dtype=np.uint32) for meta in chunk_metadata]
    num_sequences = int(sum((arr.shape[0] / 2) for arr in map_results))

    idx = np.concatenate(map_results)

    logger.info("Indexed %d sequences", num_sequences)

    return PreprocessingMetadata(metadata=idx.tobytes(), attributes={"num_sequences": num_sequences})


@CloudDataFormat(preprocessing_function=preprocess_fasta, finalizer_function=merge_fasta_metadata)
class FASTA:
    num_sequences: int


class FASTASlice(CloudObjectSlice):
    def __init__(self, offset, header, *args, **kwargs):
        self.offset = offset
        self.header = header
        super().__init__(*args, **kwargs)

    def get(self):
        buff = io.BytesIO()

        get_response = self.cloud_object.storage.get_object(
            Bucket=self.cloud_object.path.bucket,
            Key=self.cloud_object.path.key,
            Range=f"bytes={self.range_0}-{self.range_1 - 1}",
        )
        assert get_response["ResponseMetadata"]["HTTPStatusCode"] in (200, 206)

        if self.header is not None:
            header_r0, header_r1 = self.header
            header_response = self.cloud_object.storage.get_object(
                Bucket=self.cloud_object.path.bucket,
                Key=self.cloud_object.path.key,
                Range=f"bytes={header_r0}-{header_r1 - 1}",
            )
            assert get_response["ResponseMetadata"]["HTTPStatusCode"] in (200, 206)

            header_line = header_response['Body'].read()
            # Remove trailing \n and add in-sequence offset value for the first split sequence
            buff.write(header_line[:-1] + bytes(f" offset={self.offset}", 'utf-8') + b"\n")

        shutil.copyfileobj(get_response['Body'], buff)
        buff.seek(0)

        return buff.getvalue()


@PartitioningStrategy(dataformat=FASTA)
def partition_chunks_strategy(cloud_object: CloudObject, num_chunks: int):
    res = cloud_object.storage.get_object(Bucket=cloud_object.meta_path.bucket, Key=cloud_object.meta_path.key)
    idx = np.frombuffer(res['Body'].read(), dtype=np.uint32).reshape((cloud_object.attributes.num_sequences, 2))
    chunk_sz = math.ceil(cloud_object.size / num_chunks)
    ranges = [(chunk_sz * i, (chunk_sz * i) + chunk_sz) for i in range(num_chunks)]
    slices = []

    for r0, r1 in ranges:
        # Search which is the first sequence of the chunk
        seq_top_i = idx[:, 1].searchsorted(r0)
        # searchsorted returns which index would be inserted => get previous index value (seq_top_i - 1)
        seq_top_i = seq_top_i - 1 if seq_top_i > 0 else 0
        seq_top = idx[seq_top_i]
        top_id_offset, top_seq_offset = seq_top

        if top_id_offset <= r0 < top_seq_offset:
            # Chunk top splits a header line, adjust offset to include full header, offset will be 0
            r0 = top_id_offset
            offset = 0
            header = None
        else:
            # Chunk top splits a sequence, set header offset and size
            # and calculate chunked sequence offset from the beginning of the sequence
            offset = r0 - top_seq_offset
            header = (top_id_offset, top_seq_offset)

        # Search which is the last sequence of the chunk
        seq_bot_i = idx[:, 1].searchsorted(r0)
        if seq_bot_i == idx.shape[0]:
            seq_bot_i = idx.shape[0] - 1
        seq_bot = idx[seq_bot_i]
        bot_id_offset, bot_seq_offset = seq_bot

        if bot_id_offset <= r1 < bot_seq_offset:
            # If the chunk splits a header line at the bottom,
            # remove that partial header line, next chunk will handle it...
            r1 = bot_id_offset

        slices.append(FASTASlice(offset=offset, header=header, range_0=r0, range_1=r1))

    return slices
