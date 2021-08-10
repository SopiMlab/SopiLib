from __future__ import print_function

try:
    import pyext
except:
    print("ERROR: This script must be loaded by the PD/Max pyext external")

import os
import random
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np

import sopilib.gansynth_protocol as protocol
from sopilib.utils import print_err, sopimagenta_path

class gansynth(pyext._class):
    def __init__(self, *args):
        self._inlets = 1
        self._outlets = 1
        self._proc = None
        self._stderr_printer = None
        self.ganspace_components_amplitudes_buffer_name = None

    def load_1(self, ckpt_dir, batch_size=8):
        if self._proc != None:
            self.unload_1()

        python = sys.executable
        gen_script = sopimagenta_path("gansynth_worker")
        ckpt_dir = os.path.join(self._canvas_dir, str(ckpt_dir))
        worker_cmd = (python, gen_script, ckpt_dir, str(batch_size))

        print_err("starting gansynth_worker process, this may take a while")
        print_err(f"worker_cmd = {worker_cmd}")

        self._proc = subprocess.Popen(
            worker_cmd,
            stdin = subprocess.PIPE,
            stdout = subprocess.PIPE,
            stderr = subprocess.PIPE
        )
        self._stderr_printer = threading.Thread(target = self._keep_printing_stderr)
        self._stderr_printer.start()
        
        self._read_tag(protocol.OUT_TAG_INIT)
        
        info_msg = self._read(protocol.init_struct.size)
        audio_length, sample_rate = protocol.from_info_msg(info_msg)

        print("gansynth_worker is ready", file=sys.stderr)
        self._outlet(1, ["loaded", audio_length, sample_rate])


    def unload_1(self):
        if self._proc:
            self._proc.terminate()
            self._proc = None
            self._stderr_printer = None
        else:
            print("no gansynth_worker process is running", file=sys.stderr)

        self._outlet(1, "unloaded")

    def _keep_printing_stderr(self):
        while True:
            line = self._proc.stderr.readline()
            
            if not line:
                break

            sys.stderr.write("[gansynth_worker] ")
            sys.stderr.write(line.decode("utf-8"))
            sys.stderr.flush()
        
    def _write_msg(self, tag, *msgs):
        tag_msg = protocol.to_tag_msg(tag)
        self._proc.stdin.write(tag_msg)
        for msg in msgs:
            self._proc.stdin.write(msg)
        self._proc.stdin.flush()

    def _read(self, n):
        data = self._proc.stdout.read(n)
        return data
        
    def _read_tag(self, expected_tag):
        tag_msg = self._read(protocol.tag_struct.size)
        tag = protocol.from_tag_msg(tag_msg)

        if tag != expected_tag:
            raise ValueError("expected tag {}, got {}".format(expected_tag, tag))

    def load_ganspace_components_1(self, ganspace_components_file, component_amplitudes_buff_name=None):
        ganspace_components_file = os.path.join(
            self._canvas_dir,
            str(ganspace_components_file)
        )

        print("Loading GANSpace components...", file=sys.stderr)

        size_msg = protocol.to_int_msg(len(ganspace_components_file))
        components_msg = ganspace_components_file.encode('utf-8')

        self._write_msg(protocol.IN_TAG_LOAD_COMPONENTS, size_msg, components_msg)
        self._read_tag(protocol.OUT_TAG_LOAD_COMPONENTS)
        count_msg = self._read(protocol.count_struct.size)
        component_count = protocol.from_count_msg(count_msg)

        self.ganspace_components_amplitudes_buffer_name = component_amplitudes_buff_name
        if self.ganspace_components_amplitudes_buffer_name is not None:
            buf = pyext.Buffer(component_amplitudes_buff_name)
            buf.resize(component_count)
            buf.dirty()

        print("GANSpace components loaded!", file=sys.stderr)

        self._outlet(1, "loaded_pca")

    # def load_ganspace_components_v2_1(self, ganspace_components_file, component_amplitudes_buff, z_buf_name=None):
    #     ganspace_components_file = os.path.join(
    #         self._canvas_dir,
    #         str(ganspace_components_file)
    #     )

    #     print_err("Loading GANSpace v2 components...")

    #     with open(ganspace_components_file, "rb") as fp:
    #         pca = pickle.load(fp)

    #     version = pca["version"] if version in pca else 1
    #     if version < 2:
    #         raise Exception(f"can't load components - file has version {version}")

    #     comp = pca["z_comp"]
    #     n_components = comp.shape[0]
    #     comp = comp.reshape(n_components, -1) # (n_components, 1, 256) -> (n_components, 256)
    #     mean = pca["z_mean"].reshape(-1) # (1, 256) -> (256,)

    # def get_mean_z_1(self, *buf_names):
    #     in_count = len(buf_names)

    #     if in_count == 0:
    #         raise ValueError("no buffer name(s) specified")

        
        
    #     for buf_name in buf_names:
            
    def randomize_z_1(self, *buf_names):
        if not self._proc:
            raise Exception("can't randomize z - no gansynth_worker process is running")

        in_count = len(buf_names)

        if in_count == 0:
            raise ValueError("no buffer name(s) specified")
        
        in_count_msg = protocol.to_count_msg(in_count)
        self._write_msg(protocol.IN_TAG_RAND_Z, in_count_msg)
        
        self._read_tag(protocol.OUT_TAG_Z)

        out_count_msg = self._read(protocol.count_struct.size)
        out_count = protocol.from_count_msg(out_count_msg)
        
        assert out_count == in_count

        for buf_name in buf_names:
            z_msg = self._read(protocol.z_struct.size)
            z = protocol.from_z_msg(z_msg)

            z32 = z.astype(np.float32)
        
            buf = pyext.Buffer(buf_name)
            if len(buf) != len(z32):
                buf.resize(len(z32))

            buf[:] = z32
            buf.dirty()

        self._outlet(1, "randomized")

    def slerp_z_1(self, z0_name, z1_name, z_dst_name, amount):
        if not self._proc:
            raise Exception("can't slerp - no gansynth_worker process is running")

        z0_buf = pyext.Buffer(z0_name)
        z1_buf = pyext.Buffer(z1_name)
        z_dst_buf = pyext.Buffer(z_dst_name)

        z0_f64 = np.array(z0_buf, dtype=np.float64)
        z1_f64 = np.array(z1_buf, dtype=np.float64)
        
        self._write_msg(protocol.IN_TAG_SLERP_Z, protocol.to_slerp_z_msg(z0_f64, z1_f64, amount))

        self._read_tag(protocol.OUT_TAG_Z)

        out_count_msg = self._read(protocol.count_struct.size)
        out_count = protocol.from_count_msg(out_count_msg)

        assert out_count == 1

        z_msg = self._read(protocol.z_struct.size)
        z = protocol.from_z_msg(z_msg)

        z32 = z.astype(np.float32)

        if len(z_dst_buf) != len(z32):
            z_dst_buf.resize(len(z32))
        
        z_dst_buf[:] = z32
        z_dst_buf.dirty()

        self._outlet(1, "slerped")

    def synthesize_1(self, *args):
        if not self._proc:
            raise Exception("can't synthesize - no gansynth_worker process is running")
        
        arg_count = len(args)
        
        if arg_count == 0 or arg_count % 3 != 0:
            raise ValueError("invalid number of arguments ({}), should be a multiple of 3: synthesize z1 audio1 pitch1 [z2 audio2 pitch2 ...]".format(arg_count))

        if self.ganspace_components_amplitudes_buffer_name:
            component_buff = pyext.Buffer(self.ganspace_components_amplitudes_buffer_name)
            components = np.array(component_buff, dtype=np.float64)
            component_msgs = []
            for value in components:
                component_msgs.append(protocol.to_float_msg(value))
            self._write_msg(protocol.IN_TAG_SET_COMPONENT_AMPLITUDES, *component_msgs)


        gen_msgs = []
        audio_buf_names = []
        for i in range(0, arg_count, 3):
            z_buf_name, audio_buf_name, pitch = args[i:i+3]

            z32_buf = pyext.Buffer(z_buf_name)
            z = np.array(z32_buf, dtype=np.float64)
            
            gen_msgs.append(protocol.to_gen_msg(pitch, z))
            audio_buf_names.append(audio_buf_name)
            
        in_count = len(gen_msgs)
        in_count_msg = protocol.to_count_msg(in_count)
        self._write_msg(protocol.IN_TAG_GEN_AUDIO, in_count_msg, *gen_msgs)
                
        self._read_tag(protocol.OUT_TAG_AUDIO)

        out_count_msg = self._read(protocol.count_struct.size)
        out_count = protocol.from_count_msg(out_count_msg)
        
        if out_count == 0:
            return

        assert out_count == in_count

        for audio_buf_name in audio_buf_names:
            audio_size_msg = self._read(protocol.audio_size_struct.size)
            audio_size = protocol.from_audio_size_msg(audio_size_msg)

            audio_msg = self._read(audio_size)
            audio_note = protocol.from_audio_msg(audio_msg)

            audio_buf = pyext.Buffer(audio_buf_name)
            if len(audio_buf) != len(audio_note):
                audio_buf.resize(len(audio_note))

            audio_buf[:] = audio_note
            audio_buf.dirty()
        
        self._outlet(1, "synthesized")

    # expected format: synthesize_noz buf1 pitch1 [edit1_1 edit1_2 ...] -- buf2 pitch2 [...] -- [...]
    def synthesize_noz_1(self, *args):
        if not self._proc:
            raise Exception("can't synthesize - no gansynth_worker process is running")

        # parse the input
        
        init_sound = lambda: SimpleNamespace(buf=None, pitch=None, edits=[])
        
        sounds = [init_sound()]
        i = 0
        for arg in args:
            if str(arg) == "--":
                sounds.append(init_sound())
                i = 0
            else:
                if i == 0:
                    sounds[-1].buf = arg
                elif i == 1:
                    sounds[-1].pitch = arg
                else:
                    sounds[-1].edits.append(arg)

                i += 1
                
        # validate input and build synthesize messages
        
        synth_msgs = []
        for sound in sounds:
            if None in [sound.buf, sound.pitch]:
                raise ValueError("invalid syntax, should be: synthesize_noz buf1 pitch1 [edit1_1 edit1_2 ...] [-- buf2 pitch2 [edit2_1 edit2_2 ...]] [-- ...")

            edits = []
            for edit in sound.edits:
                print(f"type(edit) = {type(edit)}")
                if isinstance(edit, pyext.Symbol):
                    # edit refers to a Pd array
                    edits_buf = pyext.Buffer(edit)
                    for val in edits_buf:
                        edits.append(val)
                else:
                    # edit is a number, probably
                    edits.append(edit)
            
            synth_msgs.append(protocol.to_synthesize_noz_msg(sound.pitch, len(edits)))
            for edit in edits:
                synth_msgs.append(protocol.to_f64_msg(edit))

        # write synthesize messages
        
        in_count = len(sounds)
        in_count_msg = protocol.to_count_msg(in_count)
        self._write_msg(protocol.IN_TAG_SYNTHESIZE_NOZ, in_count_msg, *synth_msgs)
        
        # wait for output

        self._read_tag(protocol.OUT_TAG_AUDIO)

        out_count_msg = self._read(protocol.count_struct.size)
        out_count = protocol.from_count_msg(out_count_msg)
        
        assert out_count == in_count

        if out_count == 0:
            return

        for sound in sounds:
            audio_size_msg = self._read(protocol.audio_size_struct.size)
            audio_size = protocol.from_audio_size_msg(audio_size_msg)

            audio_msg = self._read(audio_size)
            audio_note = protocol.from_audio_msg(audio_msg)

            audio_buf_name = sound.buf
            audio_buf = pyext.Buffer(audio_buf_name)
            if len(audio_buf) != len(audio_note):
                audio_buf.resize(len(audio_note))

            audio_buf[:] = audio_note
            audio_buf.dirty()
        
        self._outlet(1, "synthesized")
                
    def hallucinate_1(self, *args):
        if not self._proc:
            raise Exception("can't synthesize - load a checkpoint first")

        arg_count = len(args)
        if arg_count < 3 or arg_count > 8:
            raise ValueError("invalid number of arguments ({}), should be one: hallucinate buffer_name note_count interpolation_steps".format(arg_count))

        audio_buf_name = args[0]
        note_count = int(args[1])
        interpolation_steps = int(args[2])
        rest = list(map(float, args[3:len(args)]))

        self._write_msg(protocol.IN_TAG_HALLUCINATE, protocol.to_hallucinate_msg(note_count, interpolation_steps, *rest))

        self._read_tag(protocol.OUT_TAG_AUDIO)

        audio_size_msg = self._read(protocol.audio_size_struct.size)
        audio_size = protocol.from_audio_size_msg(audio_size_msg)

        audio_msg = self._read(audio_size)
        audio_note = protocol.from_audio_msg(audio_msg)

        audio_buf = pyext.Buffer(audio_buf_name)
        if len(audio_buf) != len(audio_note):
            audio_buf.resize(len(audio_note))

        audio_buf[:] = audio_note
        audio_buf.dirty()
        
        self._outlet(1, ["hallucinated", audio_size])

    def z_mean_1(self, buf_name):
        if not self._proc:
            raise Exception("can't get z mean - no gansynth_worker process is running")

        self._write_msg(protocol.IN_TAG_GET_Z_MEAN)
        
        self._read_tag(protocol.OUT_TAG_Z)

        out_count_msg = self._read(protocol.count_struct.size)
        out_count = protocol.from_count_msg(out_count_msg)

        if out_count > 0:
            assert out_count == 1

            z_msg = self._read(protocol.z_struct.size)
            z = protocol.from_z_msg(z_msg)

            z32 = z.astype(np.float32)
        
            buf = pyext.Buffer(buf_name)
            if len(buf) != len(z32):
                buf.resize(len(z32))
            
            buf[:] = z32
            buf.dirty()
            
            self._outlet(1, ["ok", "z_mean"])

    def edit_z_1(self, src_buf_name, dst_buf_name, *edits):
        if not self._proc:
            raise Exception("can't edit z - no gansynth_worker process is running")

        src_buf = pyext.Buffer(src_buf_name)
        src_z = np.array(src_buf, dtype=np.float64)
        dst_buf = pyext.Buffer(dst_buf_name)

        raw_edits = edits
        edits = []
        for edit in raw_edits:
            if isinstance(edit, pyext.Symbol):
                # edit refers to a Pd array
                edits_buf = pyext.Buffer(edit)
                for val in edits_buf:
                    edits.append(val)
            else:
                # edit is a number, probably
                edits.append(edit)

        num_edits = len(edits)
        
        self._write_msg(
            protocol.IN_TAG_EDIT_Z,
            protocol.to_edit_z_msg(src_z, num_edits),
            *map(protocol.to_f64_msg, edits)
        )

        self._read_tag(protocol.OUT_TAG_Z)
        
        out_count_msg = self._read(protocol.count_struct.size)
        out_count = protocol.from_count_msg(out_count_msg)

        if out_count > 0:
            assert out_count == 1

            z_msg = self._read(protocol.z_struct.size)
            z = protocol.from_z_msg(z_msg)

            z32 = z.astype(np.float32)
        
            if len(dst_buf) != len(z32):
                dst_buf.resize(len(z32))
            
            dst_buf[:] = z32
            dst_buf.dirty()
            
            self._outlet(1, ["ok", "edit_z"])
