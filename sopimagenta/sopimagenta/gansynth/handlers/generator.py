import os
import random
import struct
import sys
import tensorflow.compat.v1 as tf
import numpy as np
import pickle
from types import SimpleNamespace

from magenta.models.gansynth.lib import generate_util as gu

from sopilib import gansynth_protocol as protocol
from sopilib.utils import print_err, read_msg, suppress_stdout
            
def pca_version(pca):
    return pca["version"] if "version" in pca else 1

def component_count(pca):
    if pca_version(pca) < 2:
        return len(pca["comp"])
    else:
        return len(pca["z_comp"])

def handle_rand_z(model, stdin, stdout, state):
    """
        Generates a given number of new Z coordinates.
    """
    count_msg = read_msg(stdin, protocol.count_struct.size)
    count = protocol.from_count_msg(count_msg)

    with suppress_stdout():
        zs = model.generate_z(count)
    
    stdout.write(protocol.to_tag_msg(protocol.OUT_TAG_Z))
    stdout.write(protocol.to_count_msg(len(zs)))
    
    for z in zs:
        stdout.write(protocol.to_z_msg(z))
        
    stdout.flush()

def handle_load_ganspace_components(model, stdin, stdout, state):
    size_msg = read_msg(stdin, protocol.int_struct.size)
    size = protocol.from_int_msg(size_msg)
    msg = read_msg(stdin, size)
    file = msg.decode('utf-8')
    print_err("Opening components file '{}'".format(file))
    with open(file, "rb") as fp:
        state['ganspace_components'] = pickle.load(fp)
    print_err("Components file loaded.")

    stdout.write(protocol.to_tag_msg(protocol.OUT_TAG_LOAD_COMPONENTS))
    stdout.write(protocol.to_count_msg(component_count(state["ganspace_components"])))
    stdout.flush()

def handle_set_component_amplitudes(model, stdin, stdout, state):
    amplitudes = []
    for i in range(0, component_count(state["ganspace_components"])):
        msg = read_msg(stdin, protocol.f64_struct.size)
        value = protocol.from_float_msg(msg)
        amplitudes.append(value)
    state['ganspace_component_amplitudes'] = amplitudes

def handle_slerp_z(model, stdin, stdout, state):
    slerp_z_msg = read_msg(stdin, protocol.slerp_z_struct.size)
    z0, z1, amount = protocol.from_slerp_z_msg(slerp_z_msg)

    z = gu.slerp(z0, z1, amount)

    stdout.write(protocol.to_tag_msg(protocol.OUT_TAG_Z))
    stdout.write(protocol.to_count_msg(1))
    stdout.write(protocol.to_z_msg(z))
    
    stdout.flush()
    
def handle_gen_audio(model, stdin, stdout, state):
    count_msg = read_msg(stdin, protocol.count_struct.size)
    count = protocol.from_count_msg(count_msg)
    
    pitches = []
    zs = []
    for i in range(count):
        gen_msg = read_msg(stdin, protocol.gen_audio_struct.size)
        
        pitch, z = protocol.from_gen_msg(gen_msg)
        
        pitches.append(pitch)
        zs.append(z)

    layer_offsets = {}
    if 'ganspace_component_amplitudes' in state:
        pca = state["ganspace_components"]

        components = pca['comp']
        std_devs = pca['stdev']
        edits = state['ganspace_component_amplitudes']

        amounts = np.zeros(components.shape[:1], dtype=np.float32)
        amounts[:len(list(map(float, edits)))] = edits * std_devs

        scaled_directions = amounts.reshape(-1, 1, 1, 1) * components
            
        linear_combination = np.sum(scaled_directions, axis=0)
        linear_combination_batch = np.repeat(
            linear_combination.reshape(1, *linear_combination.shape),
            8,
            axis=0
        )

        layer_offsets[pca['layer']] = linear_combination_batch

    z_arr = np.array(zs)
    try:
        with suppress_stdout():
            audios = model.generate_samples_from_z(z_arr, pitches, layer_offsets=layer_offsets)
    except KeyError as e:
        print_err("can't synthesize - model was not trained on pitch {}".format(e.args[0]))
        audios = []
        
    stdout.write(protocol.to_tag_msg(protocol.OUT_TAG_AUDIO))
    stdout.write(protocol.to_count_msg(len(audios)))

    for audio in audios:
        stdout.write(protocol.to_audio_size_msg(audio.size * audio.itemsize))
        stdout.write(protocol.to_audio_msg(audio))

    stdout.flush()
    
def handle_synthesize_noz(model, stdin, stdout, state):    
    count_msg = read_msg(stdin, protocol.count_struct.size)
    count = protocol.from_count_msg(count_msg)
    
    sounds = []
    max_num_edits = 0
    for i in range(count):
        gen_msg = read_msg(stdin, protocol.synthesize_noz_struct.size)
        
        pitch, num_edits = protocol.from_synthesize_noz_msg(gen_msg)

        max_num_edits = max(max_num_edits, num_edits)
        edits = []
        for j in range(num_edits):
            edit_msg = read_msg(stdin, protocol.f64_struct.size)
            edits.append(protocol.from_f64_msg(edit_msg))

        sounds.append(SimpleNamespace(pitch = pitch, edits = edits))

    # zero-pad all edits arrays to maximum length

    for sound in sounds:
        while len(sound.edits) < max_num_edits:
            sound.edits.append(0.0)
        
    pca = state["ganspace_components"]
    # edits = np.array(state["ganspace_component_amplitudes"], dtype=pca["stdev"].dtype)
    # edits = np.repeat([edits], len(pitches), axis=0)
    pitches = [sound.pitch for sound in sounds]
    edits = np.array([sound.edits for sound in sounds], dtype=pca["stdev"].dtype)
    
    try:
        with suppress_stdout():
            audios = model.generate_samples_from_edits(pitches, edits, pca)
    except KeyError as e:
        print_err("can't synthesize - model was not trained on pitch {}".format(e.args[0]))
        audios = []

    stdout.write(protocol.to_tag_msg(protocol.OUT_TAG_AUDIO))
    stdout.write(protocol.to_count_msg(len(audios)))

    for audio in audios:
        stdout.write(protocol.to_audio_size_msg(audio.size * audio.itemsize))
        stdout.write(protocol.to_audio_msg(audio))

    stdout.flush()

def handle_get_z_mean(model, stdin, stdout, state):
    pca = state["ganspace_components"]

    zs = []
    if pca_version(pca) < 2:
        print_err("can't get z mean from PCA result version <2")
    else:
        z = pca["z_mean"].reshape(-1)
        print_err(z.shape)
        zs = [z]
        
    stdout.write(protocol.to_tag_msg(protocol.OUT_TAG_Z))
    stdout.write(protocol.to_count_msg(len(zs)))
    for z in zs:
        stdout.write(protocol.to_z_msg(z))
    stdout.flush()

def handle_edit_z(model, stdin, stdout, state):
    pca = state["ganspace_components"]

    edit_z_msg = read_msg(stdin, protocol.edit_z_struct.size)
    in_z, num_edits = protocol.from_edit_z_msg(edit_z_msg)

    edits = []
    for i in range(num_edits):
        edit_msg = read_msg(stdin, protocol.f64_struct.size)
        edits.append(protocol.from_f64_msg(edit_msg))

    z_comp = pca["z_comp"]
    print_err(f"z_comp.shape={z_comp.shape}")
    z_comp = z_comp.reshape(-1, protocol.Z_SIZE)
    print_err(f"z_comp'.shape={z_comp.shape}")
    num_comp = z_comp.shape[0]
    
    out_zs = []
    if pca_version(pca) < 2:
        print_err("can't edit z with PCA result version <2")
    else:
        edits = edits[:num_comp]
        edits_padded = np.array(edits + [0.0]*(num_comp-len(edits)), dtype=np.float64)
        print_err(f"edits_padded.shape={edits_padded.shape}")
        
        edits_sum = np.sum(z_comp * edits_padded.reshape(-1, 1), axis=0)
        print_err(f"edits_sum.shape={edits_sum.shape}")
        out_z = in_z + edits_sum
        out_zs.append(out_z)
    
    stdout.write(protocol.to_tag_msg(protocol.OUT_TAG_Z))
    stdout.write(protocol.to_count_msg(len(out_zs)))
    for z in out_zs:
        stdout.write(protocol.to_z_msg(z))
    stdout.flush()


handlers = {
    protocol.IN_TAG_RAND_Z: handle_rand_z,
    protocol.IN_TAG_SLERP_Z: handle_slerp_z,
    protocol.IN_TAG_GEN_AUDIO: handle_gen_audio,
    protocol.IN_TAG_LOAD_COMPONENTS: handle_load_ganspace_components,
    protocol.IN_TAG_SET_COMPONENT_AMPLITUDES: handle_set_component_amplitudes,
    protocol.IN_TAG_SYNTHESIZE_NOZ: handle_synthesize_noz,
    protocol.IN_TAG_GET_Z_MEAN: handle_get_z_mean,
    protocol.IN_TAG_EDIT_Z: handle_edit_z
}
