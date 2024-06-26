import glob
import json
import math
import multiprocessing
import os
from itertools import repeat
from pathlib import Path
from typing import Dict, Tuple
import librosa

import torch
from tqdm import tqdm


def generate_overlap_vad_seq_per_file_star(args):
    """
    A workaround for tqdm with starmap of multiprocessing
    """
    return generate_overlap_vad_seq_per_file(*args)


def load_tensor_from_file(filepath: str) -> Tuple[torch.Tensor, str]:
    """
    Load torch.Tensor and the name from file
    """
    frame = []
    with open(filepath, "r", encoding='utf-8') as f:
        for line in f.readlines():
            frame.append(float(line))

    name = Path(filepath).stem
    return torch.tensor(frame), name


@torch.jit.script
def generate_overlap_vad_seq_per_tensor(
        frame: torch.Tensor, per_args: Dict[str, float], smoothing_method: str
) -> torch.Tensor:
    """
    Use generated frame prediction (generated by shifting window of shift_length_in_sec (10ms)) to generate prediction with overlapping input window/segments
    See description in generate_overlap_vad_seq.
    Use this for single instance pipeline.
    """
    # This function will be refactor for vectorization but this is okay for now

    overlap = per_args['overlap']
    window_length_in_sec = per_args['window_length_in_sec']
    shift_length_in_sec = per_args['shift_length_in_sec']
    frame_len = per_args.get('frame_len', 0.01)

    shift = int(shift_length_in_sec / frame_len)  # number of units of shift
    seg = int((window_length_in_sec / frame_len + 1))  # number of units of each window/segment

    jump_on_target = int(seg * (1 - overlap))  # jump on target generated sequence
    jump_on_frame = int(jump_on_target / shift)  # jump on input frame sequence

    if jump_on_frame < 1:
        raise ValueError(
            f"Note we jump over frame sequence to generate overlapping input segments. \n \
        Your input makes jump_on_frame={jump_on_frame} < 1 which is invalid because it cannot jump and will stuck.\n \
        Please try different window_length_in_sec, shift_length_in_sec and overlap choices. \n \
        jump_on_target = int(seg * (1 - overlap)) \n \
        jump_on_frame  = int(jump_on_frame/shift) "
        )

    target_len = int(len(frame) * shift)

    if smoothing_method == 'mean':
        preds = torch.zeros(target_len)
        pred_count = torch.zeros(target_len)

        for i, og_pred in enumerate(frame):
            if i % jump_on_frame != 0:
                continue
            start = i * shift
            end = start + seg
            preds[start:end] = preds[start:end] + og_pred
            pred_count[start:end] = pred_count[start:end] + 1

        preds = preds / pred_count
        last_non_zero_pred = preds[pred_count != 0][-1]
        preds[pred_count == 0] = last_non_zero_pred

    elif smoothing_method == 'median':
        preds = [torch.empty(0) for _ in range(target_len)]
        for i, og_pred in enumerate(frame):
            if i % jump_on_frame != 0:
                continue

            start = i * shift
            end = start + seg
            for j in range(start, end):
                if j <= target_len - 1:
                    preds[j] = torch.cat((preds[j], og_pred.unsqueeze(0)), 0)

        preds = torch.stack([torch.nanquantile(l, q=0.5) for l in preds])
        nan_idx = torch.isnan(preds)
        last_non_nan_pred = preds[~nan_idx][-1]
        preds[nan_idx] = last_non_nan_pred

    else:
        raise ValueError("smoothing_method should be either mean or median")

    return preds


def generate_overlap_vad_seq_per_file(frame_filepath: str, per_args: dict) -> str:
    """
    A wrapper for generate_overlap_vad_seq_per_tensor.
    """

    out_dir = per_args['out_dir']
    smoothing_method = per_args['smoothing_method']
    frame, name = load_tensor_from_file(frame_filepath)

    per_args_float: Dict[str, float] = {}
    for i in per_args:
        if type(per_args[i]) == float or type(per_args[i]) == int:
            per_args_float[i] = per_args[i]

    preds = generate_overlap_vad_seq_per_tensor(frame, per_args_float, smoothing_method)

    overlap_filepath = os.path.join(out_dir, name + "." + smoothing_method)
    with open(overlap_filepath, "w", encoding='utf-8') as f:
        for pred in preds:
            f.write(f"{pred:.4f}\n")

    return overlap_filepath


def generate_overlap_vad_seq(
        frame_pred_dir: str,
        smoothing_method: str,
        overlap: float,
        window_length_in_sec: float,
        shift_length_in_sec: float,
        num_workers: int,
        out_dir: str = None,
) -> str:
    """
    Generate predictions with overlapping input windows/segments. Then a smoothing filter is applied to decide the label for a frame spanned by multiple windows.
    Two common smoothing filters are supported: majority vote (median) and average (mean).
    This function uses multiprocessing to speed up.
    Args:
        frame_pred_dir (str): Directory of frame prediction file to be processed.
        smoothing_method (str): median or mean smoothing filter.
        overlap (float): amounts of overlap of adjacent windows.
        window_length_in_sec (float): length of window for generating the frame.
        shift_length_in_sec (float): amount of shift of window for generating the frame.
        out_dir (str): directory of generated predictions.
        num_workers(float): number of process for multiprocessing
    Returns:
        overlap_out_dir(str): directory of the generated predictions.
    """

    frame_filepathlist = glob.glob(frame_pred_dir + "/*.frame")
    if out_dir:
        overlap_out_dir = out_dir
    else:
        overlap_out_dir = os.path.join(
            frame_pred_dir, "overlap_smoothing_output" + "_" + smoothing_method + "_" + str(overlap)
        )

    if not os.path.exists(overlap_out_dir):
        os.mkdir(overlap_out_dir)

    per_args = {
        "overlap": overlap,
        "window_length_in_sec": window_length_in_sec,
        "shift_length_in_sec": shift_length_in_sec,
        "out_dir": overlap_out_dir,
        "smoothing_method": smoothing_method,
    }
    if num_workers is not None and num_workers > 1:
        with multiprocessing.Pool(processes=num_workers) as p:
            inputs = zip(frame_filepathlist, repeat(per_args))
            results = list(
                tqdm(
                    p.imap(generate_overlap_vad_seq_per_file_star, inputs),
                    total=len(frame_filepathlist),
                    desc='generating preds',
                    leave=True,
                )
            )

    else:
        for frame_filepath in tqdm(frame_filepathlist, desc='generating preds', leave=False):
            generate_overlap_vad_seq_per_file(frame_filepath, per_args)

    return overlap_out_dir


@torch.jit.script
def merge_overlap_segment(segments: torch.Tensor) -> torch.Tensor:
    """
    Merged the given overlapped segments.
    For example:
    torch.Tensor([[0, 1.5], [1, 3.5]]) -> torch.Tensor([0, 3.5])
    """
    if (
            segments.shape == torch.Size([0])
            or segments.shape == torch.Size([0, 2])
            or segments.shape == torch.Size([1, 2])
    ):
        return segments

    segments = segments[segments[:, 0].sort()[1]]
    merge_boundary = segments[:-1, 1] >= segments[1:, 0]
    head_padded = torch.nn.functional.pad(merge_boundary, [1, 0], mode='constant', value=0.0)
    head = segments[~head_padded, 0]
    tail_padded = torch.nn.functional.pad(merge_boundary, [0, 1], mode='constant', value=0.0)
    tail = segments[~tail_padded, 1]
    merged = torch.stack((head, tail), dim=1)
    return merged


@torch.jit.script
def binarization(sequence: torch.Tensor, per_args: Dict[str, float]) -> torch.Tensor:
    """
    Binarize predictions to speech and non-speech

    Reference
    Paper: Gregory Gelly and Jean-Luc Gauvain. "Minimum Word Error Training of RNN-based Voice Activity Detection", InterSpeech 2015.
    Implementation: https://github.com/pyannote/pyannote-audio/blob/master/pyannote/audio/utils/signal.py

    Args:
        sequence (torch.Tensor) : A tensor of frame level predictions.
        per_args:
            onset (float): onset threshold for detecting the beginning and end of a speech
            offset (float): offset threshold for detecting the end of a speech.
            pad_onset (float): adding durations before each speech segment
            pad_offset (float): adding durations after each speech segment;
            frame_length_in_sec (float): length of frame.

    Returns:
        speech_segments(torch.Tensor): A tensor of speech segment in torch.Tensor([[start1, end1], [start2, end2]]) format.
    """
    frame_length_in_sec = per_args.get('frame_length_in_sec', 0.01)

    onset = per_args.get('onset', 0.5)
    offset = per_args.get('offset', 0.5)
    pad_onset = per_args.get('pad_onset', 0.0)
    pad_offset = per_args.get('pad_offset', 0.0)

    speech = False
    start = 0.0
    i = 0

    speech_segments = torch.empty(0)

    for i in range(0, len(sequence)):
        # Current frame is speech
        if speech:
            # Switch from speech to non-speech
            if sequence[i] < offset:
                if i * frame_length_in_sec + pad_offset > max(0, start - pad_onset):
                    new_seg = torch.tensor(
                        [max(0, start - pad_onset), i * frame_length_in_sec + pad_offset]
                    ).unsqueeze(0)
                    speech_segments = torch.cat((speech_segments, new_seg), 0)

                start = i * frame_length_in_sec
                speech = False

        # Current frame is non-speech
        else:
            # Switch from non-speech to speech
            if sequence[i] > onset:
                start = i * frame_length_in_sec
                speech = True

    # if it's speech at the end, add final segment
    if speech:
        new_seg = torch.tensor([max(0, start - pad_onset), i * frame_length_in_sec + pad_offset]).unsqueeze(0)
        speech_segments = torch.cat((speech_segments, new_seg), 0)

    # Merge the overlapped speech segments due to padding
    speech_segments = merge_overlap_segment(speech_segments)  # not sorted
    return speech_segments


@torch.jit.script
def filter_short_segments(segments: torch.Tensor, threshold: float) -> torch.Tensor:
    """
    Remove segments which duration is smaller than a threshold.
    For example,
    torch.Tensor([[0, 1.5], [1, 3.5], [4, 7]]) and threshold = 2.0
    ->
    torch.Tensor([[1, 3.5], [4, 7]])
    """
    return segments[segments[:, 1] - segments[:, 0] >= threshold]


@torch.jit.script
def remove_segments(original_segments: torch.Tensor, to_be_removed_segments: torch.Tensor) -> torch.Tensor:
    """
    Remove speech segments list in to_be_removed_segments from original_segments.
    For example,
    remove torch.Tensor([[start2, end2],[start4, end4]]) from torch.Tensor([[start1, end1],[start2, end2],[start3, end3], [start4, end4]]),
    ->
    torch.Tensor([[start1, end1],[start3, end3]])
    """
    for y in to_be_removed_segments:
        original_segments = original_segments[original_segments.eq(y).all(dim=1).logical_not()]
    return original_segments


@torch.jit.script
def get_gap_segments(segments: torch.Tensor) -> torch.Tensor:
    """
    Get the gap segments.
    For example,
    torch.Tensor([[start1, end1], [start2, end2], [start3, end3]]) -> torch.Tensor([[end1, start2], [end2, start3]])
    """
    segments = segments[segments[:, 0].sort()[1]]
    return torch.column_stack((segments[:-1, 1], segments[1:, 0]))


@torch.jit.script
def filtering(speech_segments: torch.Tensor, per_args: Dict[str, float]) -> torch.Tensor:
    """
    Filter out short non_speech and speech segments.

    Reference
    Paper: Gregory Gelly and Jean-Luc Gauvain. "Minimum Word Error Training of RNN-based Voice Activity Detection", InterSpeech 2015.
    Implementation: https://github.com/pyannote/pyannote-audio/blob/master/pyannote/audio/utils/signal.py
    Args:
        speech_segments (torch.Tensor):  A tensor of speech segment in torch.Tensor([[start1, end1], [start2, end2]]) format.
        per_args:
            min_duration_on (float): threshold for small non_speech deletion
            min_duration_off (float): threshold for short speech segment deletion
            filter_speech_first (float): Whether to perform short speech segment deletion first. Use 1.0 to represent True.

    Returns:
        speech_segments(torch.Tensor): A tensor of filtered speech segment in torch.Tensor([[start1, end1], [start2, end2]]) format.
    """
    if speech_segments.shape == torch.Size([0]):
        return speech_segments

    min_duration_on = per_args.get('min_duration_on', 0.0)
    min_duration_off = per_args.get('min_duration_off', 0.0)
    filter_speech_first = per_args.get('filter_speech_first', 1.0)

    if filter_speech_first == 1.0:
        # Filter out the shorter speech segments
        if min_duration_on > 0.0:
            speech_segments = filter_short_segments(speech_segments, min_duration_on)
        # Filter out the shorter non-speech segments and return to be as speech segments
        if min_duration_off > 0.0:
            # Find non-speech segments
            non_speech_segments = get_gap_segments(speech_segments)
            # Find shorter non-speech segments
            short_non_speech_segments = remove_segments(
                non_speech_segments, filter_short_segments(non_speech_segments, min_duration_off)
            )
            # Return shorter non-speech segments to be as speech segments
            speech_segments = torch.cat((speech_segments, short_non_speech_segments), 0)

            # Merge the overlapped speech segments
            speech_segments = merge_overlap_segment(speech_segments)
    else:
        if min_duration_off > 0.0:
            # Find non-speech segments
            non_speech_segments = get_gap_segments(speech_segments)
            # Find shorter non-speech segments
            short_non_speech_segments = remove_segments(
                non_speech_segments, filter_short_segments(non_speech_segments, min_duration_off)
            )

            speech_segments = torch.cat((speech_segments, short_non_speech_segments), 0)

            # Merge the overlapped speech segments
            speech_segments = merge_overlap_segment(speech_segments)
        if min_duration_on > 0.0:
            speech_segments = filter_short_segments(speech_segments, min_duration_on)

    return speech_segments

def percentile(data: torch.Tensor, perc: int) -> float:
    """
    Calculate percentile given data
    """
    size = len(data)
    return float(sorted(data)[int(math.ceil((size * perc) / 100)) - 1])

def cal_vad_onset_offset(
    scale: str, onset: float, offset: float, sequence: torch.Tensor = None
) -> Tuple[float, float]:
    """
    Calculate onset and offset threshold given different scale.
    """
    if scale == "absolute":
        mini = 0
        maxi = 1
    elif scale == "relative":
        mini = min(sequence)
        maxi = max(sequence)
    elif scale == "percentile":
        mini = percentile(sequence, 1)
        maxi = percentile(sequence, 99)

    onset = mini + onset * (maxi - mini)
    offset = mini + offset * (maxi - mini)
    return float(onset), float(offset)

@torch.jit.script
def generate_vad_segment_table_per_tensor(sequence: torch.Tensor, per_args: Dict[str, float]) -> torch.Tensor:
    """
    See description in generate_overlap_vad_seq.
    Use this for single instance pipeline.
    """
    UNIT_FRAME_LEN = 0.01

    speech_segments = binarization(sequence, per_args)
    speech_segments = filtering(speech_segments, per_args)

    if speech_segments.shape == torch.Size([0]):
        return speech_segments

    speech_segments, _ = torch.sort(speech_segments, 0)

    dur = speech_segments[:, 1:2] - speech_segments[:, 0:1] + UNIT_FRAME_LEN
    speech_segments = torch.column_stack((speech_segments, dur))

    return speech_segments


def prepare_gen_segment_table(sequence: torch.Tensor, per_args: dict) -> Tuple[str, dict]:
    """
    Preparing for generating segment table.
    """
    out_dir = per_args.get('out_dir', "")

    # calculate onset offset based on scale selection
    per_args['onset'], per_args['offset'] = cal_vad_onset_offset(
        per_args.get('scale', 'absolute'), per_args['onset'], per_args['offset'], sequence
    )

    # cast 'filter_speech_first' for torch.jit.script
    if 'filter_speech_first' in per_args:
        if per_args['filter_speech_first']:
            per_args['filter_speech_first'] = 1.0
        else:
            per_args['filter_speech_first'] = 0.0

    per_args_float: Dict[str, float] = {}
    for i in per_args:
        if type(per_args[i]) == float or type(per_args[i]) == int:
            per_args_float[i] = per_args[i]

    return out_dir, per_args_float


def generate_vad_segment_table_per_file(pred_filepath: str, per_args: dict) -> str:
    """
    A wrapper for generate_vad_segment_table_per_tensor
    """
    sequence, name = load_tensor_from_file(pred_filepath)
    out_dir, per_args_float = prepare_gen_segment_table(sequence, per_args)

    preds = generate_vad_segment_table_per_tensor(sequence, per_args_float)
    ext = ".rttm" if per_args.get("use_rttm", False) else ".txt"
    save_name = name + ext
    save_path = os.path.join(out_dir, save_name)

    if preds.shape[0] == 0:
        with open(save_path, "w", encoding='utf-8') as fp:
            if per_args.get("use_rttm", False):
                fp.write(f"SPEAKER <NA> 1 0 0 <NA> <NA> speech <NA> <NA>\n")
            else:
                fp.write(f"0 0 speech\n")
    else:
        with open(save_path, "w", encoding='utf-8') as fp:
            for i in preds:
                if per_args.get("use_rttm", False):
                    fp.write(f"SPEAKER {name} 1 {i[0]:.4f} {i[2]:.4f} <NA> <NA> speech <NA> <NA>\n")
                else:
                    fp.write(f"{i[0]:.4f} {i[2]:.4f} speech\n")

    return save_path


def generate_vad_segment_table(
        vad_pred_dir: str,
        postprocessing_params: dict,
        frame_length_in_sec: float,
        num_workers: int,
        out_dir: str = None,
        use_rttm: bool = False,
) -> str:
    """
    Convert frame level prediction to speech segment in start and end times format.
    And save to csv file  in rttm-like format
            0, 10, speech
            17,18, speech
    Args:
        vad_pred_dir (str): directory of prediction files to be processed.
        postprocessing_params (dict): dictionary of thresholds for prediction score. See details in binarization and filtering.
        frame_length_in_sec (float): frame length.
        out_dir (str): output dir of generated table/csv file.
        num_workers(float): number of process for multiprocessing
    Returns:
        out_dir(str): directory of the generated table.
    """

    suffixes = ("frame", "mean", "median")
    vad_pred_filepath_list = [os.path.join(vad_pred_dir, x) for x in os.listdir(vad_pred_dir) if x.endswith(suffixes)]

    if not out_dir:
        out_dir_name = "seg_output"
        for key in postprocessing_params:
            out_dir_name = out_dir_name + "-" + str(key) + str(postprocessing_params[key])

        out_dir = os.path.join(vad_pred_dir, out_dir_name)

    if not os.path.exists(out_dir):
        os.mkdir(out_dir)

    per_args = {
        "frame_length_in_sec": frame_length_in_sec,
        "out_dir": out_dir,
        "use_rttm": use_rttm,
    }
    per_args = {**per_args, **postprocessing_params}
    num_workers = None
    if num_workers is not None and num_workers > 1:
        with multiprocessing.Pool(num_workers) as p:
            inputs = zip(vad_pred_filepath_list, repeat(per_args))
            list(
                tqdm(
                    p.imap(generate_vad_segment_table_per_file_star, inputs),
                    total=len(vad_pred_filepath_list),
                    desc='creating speech segments',
                    leave=True,
                )
            )
    else:
        for vad_pred_filepath in tqdm(vad_pred_filepath_list, desc='creating speech segments', leave=True):
            generate_vad_segment_table_per_file(vad_pred_filepath, per_args)

    return out_dir

def generate_vad_segment_table_per_file_star(args):
    """
    A workaround for tqdm with starmap of multiprocessing
    """
    return generate_vad_segment_table_per_file(*args)

def get_vad_stream_status(data: list) -> list:
    """
    Generate a list of status for each snippet in manifest. A snippet should be in single, start, next or end status.
    Used for concatenating to full audio file.
    Args:
        data (list): list of filepath of audio snippet
    Returns:
        status (list): list of status of each snippet.
    """
    if len(data) == 1:
        return ['single']

    status = [None] * len(data)
    for i in range(len(data)):
        if i == 0:
            status[i] = 'start' if data[i] == data[i + 1] else 'single'
        elif i == len(data) - 1:
            status[i] = 'end' if data[i] == data[i - 1] else 'single'
        else:
            if data[i] != data[i - 1] and data[i] == data[i + 1]:
                status[i] = 'start'
            elif data[i] == data[i - 1] and data[i] == data[i + 1]:
                status[i] = 'next'
            elif data[i] == data[i - 1] and data[i] != data[i + 1]:
                status[i] = 'end'
            else:
                status[i] = 'single'
    return status


def write_vad_infer_manifest(file: dict, args_func: dict) -> list:
    """
    Used by prepare_manifest.
    Given a list of files, split them with maximum split_duration and write them to the manifest.
    Args:
        files (dict) : file to be processed
        args_func:
            label (str): label for audio snippet.y
            split_duration (float): max duration of each audio clip (each line in json)
            window_length_in_sec (float) : length of window for generating the frame. Used for taking care of joint.
    Returns:
        res (list) : list of generated metadata line of json for file
    """
    res = []
    label = args_func['label']
    split_duration = args_func['split_duration']
    window_length_in_sec = args_func['window_length_in_sec']
    filepath = file['audio_filepath']
    in_duration = file.get('duration', None)
    in_offset = file.get('offset', 0)

    # if filepath is not found, try to find it in the dir of manifest
    if not Path(filepath).is_file():
        new_filepath = Path(args_func['manifest_dir']) / filepath
        if new_filepath.is_file():
            filepath = new_filepath.absolute().as_posix()

    try:
        sr = 16000
        x, _sr = librosa.load(filepath, sr=sr, offset=in_offset, duration=in_duration)
        duration = librosa.get_duration(y=x, sr=sr)
        left = duration
        current_offset = in_offset

        status = 'single'
        while left > 0:
            if left <= split_duration:
                if status == 'single':
                    write_duration = left
                    current_offset = 0
                else:
                    status = 'end'
                    write_duration = left + window_length_in_sec
                    current_offset -= window_length_in_sec
                offset_inc = left
                left = 0
            else:
                if status == 'start' or status == 'next':
                    status = 'next'
                else:
                    status = 'start'

                if status == 'start':
                    write_duration = split_duration
                    offset_inc = split_duration
                else:
                    write_duration = split_duration + window_length_in_sec
                    current_offset -= window_length_in_sec
                    offset_inc = split_duration + window_length_in_sec

                left -= split_duration

            metadata = {
                'audio_filepath': filepath,
                'duration': write_duration,
                'label': label,
                'text': '_',
                'offset': current_offset,
            }
            res.append(metadata)

            current_offset += offset_inc

    except Exception as e:
        err_file = "error.log"
        with open(err_file, 'w', encoding='utf-8') as fout:
            fout.write(filepath + ":" + str(e))
    return res


def write_vad_infer_manifest_star(args):
    """
    A workaround for tqdm with starmap of multiprocessing
    """
    return write_vad_infer_manifest(*args)

def prepare_manifest(config: dict) -> str:
    """
    Perform VAD on long audio snippet might cause CUDA out of memory issue.
    Automatically split manifest entry by split_duration to avoid the potential memory issue.
    """
    if 'prepared_manifest_vad_input' in config and config['prepared_manifest_vad_input']:
        manifest_vad_input = config['prepared_manifest_vad_input']
    else:
        default_path = "manifest_vad_input.json"
        manifest_vad_input = os.path.join(config["out_dir"], default_path) if "out_dir" in config else default_path

    # input_list is a list of variable ['audio_filepath': i, "offset": xxx, "duration": xxx])
    if type(config['input']) == str:
        input_list = []
        with open(config['input'], 'r', encoding='utf-8') as manifest:
            for line in manifest.readlines():
                input_list.append(json.loads(line.strip()))
    elif type(config['input']) == list:
        input_list = config['input']
    else:
        raise ValueError(
            "The input for manifest preparation would either be a string of the filepath to manifest or a list of {'audio_filepath': i, 'offset': 0, 'duration': null} "
        )

    args_func = {
        'label': 'infer',
        'split_duration': config['split_duration'],
        'window_length_in_sec': config['window_length_in_sec'],
        'manifest_dir': Path(config['input']).parent if type(config['input']) == str else '',
    }

    if config.get('num_workers') is not None and config['num_workers'] > 1:
        with multiprocessing.Pool(processes=config['num_workers']) as p:
            inputs = zip(input_list, repeat(args_func))
            results = list(
                tqdm(
                    p.imap(write_vad_infer_manifest_star, inputs),
                    total=len(input_list),
                    desc='splitting manifest',
                    leave=True,
                )
            )
    else:
        results = [
            write_vad_infer_manifest(input_el, args_func)
            for input_el in tqdm(input_list, desc='splitting manifest', leave=True)
        ]

    if os.path.exists(manifest_vad_input):
        print("The prepared manifest file exists. Overwriting!")
        os.remove(manifest_vad_input)

    with open(manifest_vad_input, 'a', encoding='utf-8') as fout:
        for res in results:
            for r in res:
                json.dump(r, fout)
                fout.write('\n')
                fout.flush()
    return manifest_vad_input