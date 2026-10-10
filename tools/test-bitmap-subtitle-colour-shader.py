#!/usr/bin/env python3
"""Render the production GLES fragment on a host surfaceless EGL implementation.

Checks neutral/alpha/SDR tuning, transfer variants, and unchanged PQ conversion.
Uses ctypes plus existing EGL/GL dispatch libraries; installs nothing. This is
host shader execution, not Kodi GUI, Mali performance or HDMI verification.
"""
import argparse
import ctypes as C
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = 'system/shaders/GLES/2.0/gles_shader_texture_noblend.frag'
I, U, F, P = C.c_int, C.c_uint, C.c_float, C.c_void_p


class GLES:
    def __init__(self):
        self.egl = C.CDLL('libEGL.so.1')
        self.proc = self.ef('eglGetProcAddress', P, [C.c_char_p])
        self.display = self.ef('eglGetDisplay', P, [P])(None)
        major, minor = I(), I()
        assert self.ef('eglInitialize', U, [P, C.POINTER(I), C.POINTER(I)])(
            self.display, C.byref(major), C.byref(minor)), 'EGL initialization failed'
        assert self.ef('eglBindAPI', U, [U])(0x30A0)
        attrs = (I * 13)(0x3033, 1, 0x3040, 4, 0x3024, 8, 0x3023, 8,
                         0x3022, 8, 0x3021, 8, 0x3038)
        config, count = P(), I()
        assert self.ef('eglChooseConfig', U, [P, C.POINTER(I), C.POINTER(P), I, C.POINTER(I)])(
            self.display, attrs, C.byref(config), 1, C.byref(count)) and count.value
        self.surface = self.ef('eglCreatePbufferSurface', P, [P, P, C.POINTER(I)])(
            self.display, config, (I * 5)(0x3057, 1, 0x3056, 1, 0x3038))
        self.context = self.ef('eglCreateContext', P, [P, P, P, C.POINTER(I)])(
            self.display, config, None, (I * 3)(0x3098, 2, 0x3038))
        assert self.surface and self.context
        assert self.ef('eglMakeCurrent', U, [P, P, P, P])(
            self.display, self.surface, self.surface, self.context)
        self.fn = {}
        signatures = {
            'glGetString': (C.c_char_p, [U]),
            'glCreateShader': (U, [U]),
            'glShaderSource': (None, [U, I, C.POINTER(C.c_char_p), C.POINTER(I)]),
            'glCompileShader': (None, [U]),
            'glGetShaderiv': (None, [U, U, C.POINTER(I)]),
            'glGetShaderInfoLog': (None, [U, I, C.POINTER(I), C.c_char_p]),
            'glCreateProgram': (U, []),
            'glAttachShader': (None, [U, U]),
            'glLinkProgram': (None, [U]),
            'glGetProgramiv': (None, [U, U, C.POINTER(I)]),
            'glGetProgramInfoLog': (None, [U, I, C.POINTER(I), C.c_char_p]),
            'glDeleteShader': (None, [U]),
            'glDeleteProgram': (None, [U]),
            'glUseProgram': (None, [U]),
            'glGetUniformLocation': (I, [U, C.c_char_p]),
            'glUniform1f': (None, [I, F]),
            'glUniform1i': (None, [I, I]),
            'glGenTextures': (None, [I, C.POINTER(U)]),
            'glBindTexture': (None, [U, U]),
            'glTexParameteri': (None, [U, U, I]),
            'glTexImage2D': (None, [U, I, I, I, I, I, U, U, P]),
            'glGetAttribLocation': (I, [U, C.c_char_p]),
            'glVertexAttribPointer': (None, [U, I, U, U, I, P]),
            'glEnableVertexAttribArray': (None, [U]),
            'glDrawArrays': (None, [U, I, I]),
            'glReadPixels': (None, [I, I, I, I, U, U, P]),
            'glViewport': (None, [I, I, I, I]),
            'glDisable': (None, [U]),
            'glGetError': (U, []),
        }
        for name, (result, args) in signatures.items():
            address = self.proc(name.encode())
            assert address, name
            self.fn[name] = C.CFUNCTYPE(result, *args)(address)
        self.glDisable(0x0BD0)  # deterministic 8-bit readback without dithering
        self.glViewport(0, 0, 1, 1)
        self.texture = U()
        self.glGenTextures(1, C.byref(self.texture))
        self.glBindTexture(0x0DE1, self.texture)
        self.glTexParameteri(0x0DE1, 0x2800, 0x2600)
        self.glTexParameteri(0x0DE1, 0x2801, 0x2600)
        self.vertices = (F * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
        self.programs = []

    def __getattr__(self, name):
        return self.fn[name]

    def ef(self, name, result, args):
        f = getattr(self.egl, name)
        f.restype, f.argtypes = result, args
        return f

    def shader(self, source, kind):
        shader = self.glCreateShader(kind)
        data = C.c_char_p(source.encode())
        self.glShaderSource(shader, 1, C.byref(data), None)
        self.glCompileShader(shader)
        ok = I()
        self.glGetShaderiv(shader, 0x8B81, C.byref(ok))
        log = C.create_string_buffer(16384)
        self.glGetShaderInfoLog(shader, len(log), None, log)
        # Compilation/link failure must not count as a rejected behaviour mutant.
        if not ok.value:
            raise RuntimeError(log.value.decode())
        return shader

    def program(self, fragment, defines=''):
        # Match CGLESShader's shared-source insertion without changing old oracles.
        if 'guiColour(' in fragment and 'vec3 guiColour(' not in fragment:
            fragment = fragment.replace('void main',
                (ROOT / 'system/shaders/GLES/2.0/gui_colour.frag').read_text() + '\nvoid main', 1)
        vertex = self.shader('''#version 100
attribute vec2 position;
varying vec4 m_cord0;
void main(){gl_Position=vec4(position,0.0,1.0);m_cord0=vec4(0.5,0.5,0.0,1.0);}
''', 0x8B31)
        fragment = fragment.replace('#version 100', '#version 100\n' + defines, 1)
        pixel = self.shader(fragment, 0x8B30)
        program = self.glCreateProgram()
        self.glAttachShader(program, vertex)
        self.glAttachShader(program, pixel)
        self.glLinkProgram(program)
        ok = I()
        self.glGetProgramiv(program, 0x8B82, C.byref(ok))
        log = C.create_string_buffer(16384)
        self.glGetProgramInfoLog(program, len(log), None, log)
        # Compilation/link failure must not count as a rejected behaviour mutant.
        if not ok.value:
            raise RuntimeError(log.value.decode())
        self.glDeleteShader(vertex); self.glDeleteShader(pixel)
        self.programs.append(program)
        return program

    def pixel(self, program, rgba, brightness=1, saturation=1, pma=1, peak=1, pqmode=0, tonemap=0, subtitlepeak=1):
        self.glUseProgram(program)
        for name, value in [('m_sdrBrightness', brightness), ('m_sdrSaturation', saturation),
                            ('m_subtitlePeak', subtitlepeak), ('m_pma', pma), ('m_sdrPeak', peak), ('m_pqRefNits', 203),
                            ('m_pqSaturation', 1.25), ('m_pqTonemap', tonemap), ('m_pqMode', pqmode)]:
            self.glUniform1f(self.glGetUniformLocation(program, name.encode()), value)
        self.glUniform1i(self.glGetUniformLocation(program, b'm_samp0'), 0)
        data = (C.c_ubyte * 4)(*rgba)
        self.glTexImage2D(0x0DE1, 0, 0x1908, 1, 1, 0, 0x1908, 0x1401, data)
        pos = self.glGetAttribLocation(program, b'position')
        self.glVertexAttribPointer(pos, 2, 0x1406, 0, 0, self.vertices)
        self.glEnableVertexAttribArray(pos)
        self.glDrawArrays(0x0005, 0, 4)
        output = (C.c_ubyte * 4)()
        self.glReadPixels(0, 0, 1, 1, 0x1908, 0x1401, output)
        assert self.glGetError() == 0
        return tuple(output)

    def close(self):
        for program in self.programs:
            self.glDeleteProgram(program)
        self.ef('eglMakeCurrent', U, [P, P, P, P])(self.display, None, None, None)
        self.ef('eglDestroyContext', U, [P, P])(self.display, self.context)
        self.ef('eglDestroySurface', U, [P, P])(self.display, self.surface)
        self.ef('eglTerminate', U, [P])(self.display)


def encoded(rgb, alpha):
    return tuple(round((c ** 2.2 * alpha / 255) ** (1 / 2.2) * 255) for c in rgb) + (alpha,)


def check(gl, fragment, baseline):
    programs = [gl.program(fragment, ('#define KODI_LIMITED_RANGE 1\n' if v & 1 else '') +
                           ('#define KODI_TRANSFER_PQ 1\n' if v & 2 else '')) for v in range(4)]
    originals = [gl.program(baseline, ('#define KODI_LIMITED_RANGE 1\n' if v & 1 else '') +
                            ('#define KODI_TRANSFER_PQ 1\n' if v & 2 else '')) for v in range(4)] if baseline else []
    count = 0
    for alpha in [0, 1, 16, 64, 128, 192, 255]:
        for colour in [(1, 1, 1), (0.5, 0.5, 0.5), (0.9, 0.3, 0.1), (0.05, 0.8, 0.2)]:
            texel = encoded(colour, alpha)
            neutral = gl.pixel(programs[0], texel)
            assert neutral == texel, (texel, neutral)
            if originals:
                for v in range(4):
                    assert gl.pixel(programs[v], texel, peak=0.75) == gl.pixel(originals[v], texel, peak=0.75)
            for brightness, saturation in [(0, 1), (0.5, 1), (2, 1), (1, 0), (1, 2), (0.5, 2)]:
                got = gl.pixel(programs[0], texel, brightness, saturation)
                # Independent authored-colour oracle; reconstruct alpha after tuning,
                # rather than dividing the stored gamma-encoded RGB by alpha.
                linear = [c ** 2.2 for c in colour]
                luma = sum(x * weight for x, weight in zip(linear, [0.2126, 0.7152, 0.0722]))
                adjusted = [min(1, max(0, (luma + (x - luma) * saturation) * brightness)) for x in linear]
                expected = tuple(round((x * alpha / 255) ** (1 / 2.2) * 255) for x in adjusted) + (alpha,)
                assert all(abs(a - b) <= 2 for a, b in zip(got, expected)), (texel, brightness, saturation, got, expected)
                assert got[3] == alpha
                if saturation == 0:
                    assert max(got[:3]) - min(got[:3]) <= 1
                for v in range(1, 4):
                    transformed = gl.pixel(programs[v], texel, brightness, saturation, peak=0.75)
                    expected_transform = [x / 255 for x in got[:3]]
                    if v & 1:
                        expected_transform = [x * 219 / 255 + alpha / 255 * 16 / 255 for x in expected_transform]
                    if v & 2:
                        expected_transform = [x * 0.75 for x in expected_transform]
                    assert all(abs(transformed[i] - round(expected_transform[i] * 255)) <= 1 for i in range(3))
                    assert transformed[3] == alpha
                count += 1
    # Neutral defaults skip tuning even for straight-alpha generic GUI textures.
    assert gl.pixel(programs[0], (80, 100, 120, 64), pma=0) == (80, 100, 120, 64)
    pq = gl.program(fragment, '#define KODI_PQ_TO_SDR 1\n')
    original_pq = gl.program(baseline, '#define KODI_PQ_TO_SDR 1\n') if baseline else None
    for mode in range(3):
        for tonemap in [0, 1]:
            for rgba in [(0, 0, 0, 0), (64, 40, 20, 128), (128, 170, 200, 255)]:
                neutral = gl.pixel(pq, rgba, pqmode=mode, tonemap=tonemap)
                assert neutral == gl.pixel(pq, rgba, 0.5, 0, pqmode=mode, tonemap=tonemap)
                if original_pq:
                    assert neutral == gl.pixel(original_pq, rgba, pqmode=mode, tonemap=tonemap)
    return count



def check_output(gl, fragment):
    programs = [gl.program(fragment, ('#define KODI_LIMITED_RANGE 1\n' if v & 1 else '') +
                           ('#define KODI_TRANSFER_PQ 1\n' if v & 2 else '')) for v in range(4)]
    pq = gl.program(fragment, '#define KODI_PQ_TO_SDR 1\n')
    count = 0
    for alpha in [0, 1, 16, 64, 128, 192, 255]:
        for colour in [(1, 1, 1), (0.25, 0.25, 0.25), (0.9, 0.3, 0.1), (0.05, 0.8, 0.2)]:
            texel = encoded(colour, alpha)
            for brightness, saturation in [(0.5, 1), (2, 1), (2, 0)]:
                # The independent authored-linear oracle chooses output white
                # AFTER source tuning and clipping, then premultiplies/encodes.
                linear = [c ** 2.2 for c in colour]
                luma = sum(x * w for x, w in zip(linear, [0.2126, 0.7152, 0.0722]))
                adjusted = [min(1, max(0, (luma + (x - luma) * saturation) * brightness)) for x in linear]
                for white in [0, 0.1, 0.5, 1]:
                    for gui_peak in [0.3, 1]:
                        for v in range(4):
                            got = gl.pixel(programs[v], texel, brightness, saturation,
                                           peak=gui_peak, subtitlepeak=white ** (1 / 2.2))
                            expected = [(x * white * alpha / 255) ** (1 / 2.2) for x in adjusted]
                            if v & 1:
                                expected = [x * 219 / 255 + alpha / 255 * 16 / 255 for x in expected]
                            if v & 2:
                                expected = [x * gui_peak for x in expected]
                            assert all(abs(got[i] - round(expected[i] * 255)) <= 2 for i in range(3)), (texel, brightness, white, v, got, expected)
                            assert got[3] == alpha
                            count += 1
    # Input gain clips the source first: reducing it cannot replace a separate
    # output white. A boosted middle tone remains at selected output white.
    mid = encoded((0.8, 0.8, 0.8), 255)
    output_white = gl.pixel(programs[0], mid, brightness=2, subtitlepeak=0.5 ** (1 / 2.2))
    input_only = gl.pixel(programs[0], mid, brightness=1)
    assert output_white[0] < input_only[0] - 8
    # The same white scale applies before a downstream composite/global GUI
    # stage: it remains RELATIVE to that stage, not an absolute nit override.
    for alpha in [0, 64, 255]:
        texel = encoded((0.9, 0.3, 0.1), alpha)
        for global_peak in [0.3, 0.75, 1]:
            relative = gl.pixel(programs[0], texel, subtitlepeak=0.5 ** (1 / 2.2))
            downstream = tuple(round(x * global_peak) for x in relative[:3]) + (alpha,)
            primitive = gl.pixel(programs[2], texel, peak=global_peak, subtitlepeak=0.5 ** (1 / 2.2))
            assert all(abs(a - b) <= 1 for a, b in zip(downstream, primitive))
    caps = 0
    for alpha in [0, 16, 64, 128, 255]:
        # PQ textures are plain-PMA. Include bright saturated and dark colours.
        for authored in [(0.8, 0.5, 0.3), (0.45, 0.3, 0.2), (0.1, 0.1, 0.1)]:
            texel = tuple(round(x * alpha) for x in authored) + (alpha,)
            for mode in range(3):
                for tonemap in [0, 1]:
                    uncapped = gl.pixel(pq, texel, pqmode=mode, tonemap=tonemap)
                    for white in [0, 0.1, 0.5, 1]:
                        ceiling = alpha * white ** (1 / 2.3)
                        got = gl.pixel(pq, texel, pqmode=mode, tonemap=tonemap,
                                       subtitlepeak=white ** (1 / 2.3))
                        assert got[3] == alpha
                        assert max(got[:3]) <= ceiling + 1, (texel, white, got, ceiling)
                        if max(uncapped[:3]) <= ceiling:
                            assert got == uncapped  # lower colours are not dimmed
                        elif white > 0:
                            assert abs(max(got[:3]) - ceiling) <= 1
                            # A hue-preserving cap scales every channel together.
                            # Cross-products avoid unstable division at dark edges.
                            hi = max(range(3), key=lambda i: uncapped[i])
                            for i in range(3):
                                assert abs(got[i] * uncapped[hi] - uncapped[i] * got[hi]) <= 2 * uncapped[hi]
                        if white == 1:
                            assert got == uncapped
                        caps += 1
    return count, caps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', help='Compare neutral and PQ output against this Git revision')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    fragment = (ROOT / FRAGMENT).read_text()
    baseline = subprocess.check_output(['git', 'show', args.baseline + ':' + FRAGMENT], cwd=ROOT, text=True) if args.baseline else None
    with tempfile.TemporaryDirectory(prefix='bitmap-colour-shader-') as tmp:
        os.environ.update(EGL_PLATFORM='surfaceless', LIBGL_ALWAYS_SOFTWARE='1', MESA_SHADER_CACHE_DIR=tmp)
        gl = GLES()
        try:
            count = check(gl, fragment, baseline)
            print('PASS:', count, 'SDR alpha/tuning cases + four transfer variants and PQ modes;', gl.glGetString(0x1F01).decode())
            output_count, caps = check_output(gl, fragment)
            print('PASS:', output_count, 'SDR output-white/transfer cases and', caps, 'corrected-PGS alpha/hue ceiling cases')
            if args.negative_controls:
                for label, old, new in [
                    ('ceiling ignores alpha', 'rgb.a * m_subtitlePeak /', 'm_subtitlePeak /'),
                    ('ceiling dims dark colours', 'min(1.0, rgb.a * m_subtitlePeak / max(peak, 1e-6))', 'm_subtitlePeak'),
                    ('ceiling changes colour ratios', 'rgb.rgb *= min(1.0, rgb.a * m_subtitlePeak / max(peak, 1e-6));', 'rgb.rgb = min(rgb.rgb, vec3(rgb.a * m_subtitlePeak));'),
                    ('output white applied twice', 'rgb.rgb *= m_subtitlePeak;', 'rgb.rgb *= m_subtitlePeak * m_subtitlePeak;'),
                    ('output white missing', 'rgb.rgb *= m_subtitlePeak;', 'rgb.rgb *= 1.0;'),
                    ('PMA alpha clamp omitted', 'vec3(ceiling));', 'vec3(1.0));'),
                    ('gamma instead of linear', 'vec3(2.2)', 'vec3(1.0)'),
                    ('alpha modified', 'gl_FragColor = rgb;', 'rgb.a *= m_pma * 0.5 + 0.5; gl_FragColor = rgb;'),
                    ('saturation ignored', 'linear, m_sdrSaturation)', 'linear, 1.0)'),
                ]:
                    assert old in fragment
                    try:
                        mutant = fragment.replace(old, new)
                        check(gl, mutant, baseline)
                        check_output(gl, mutant)
                    except AssertionError:
                        print('REJECTED:', label)
                    else:
                        raise AssertionError('Unsafe shader accepted: ' + label)
                # Move the intact block before input tuning: it still compiles,
                # but changes the result when input brightness would clip.
                cap = fragment.index('  // A corrected-PGS ceiling')
                start = fragment.rfind('#if defined(KODI_PQ_TO_SDR)', 0, cap)
                end = fragment.index('#if defined(KODI_LIMITED_RANGE)', cap)
                block = fragment[start:end]
                mutant = fragment[:start] + fragment[end:]
                before = mutant.index('#if !defined(KODI_PQ_TO_SDR)\n  // Normal bitmap')
                mutant = mutant[:before] + block + mutant[before:]
                try:
                    check_output(gl, mutant)
                except AssertionError:
                    print('REJECTED: output white before input clipping')
                else:
                    raise AssertionError('Unsafe output ordering accepted')
        finally:
            gl.close()


if __name__ == '__main__':
    main()
