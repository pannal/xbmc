#!/usr/bin/env python3
"""Execute GUI fragments on host GLES: defaults, colour math and overlay isolation.

Uses production fragments/shared source and links both production GUI vertices.
Host software EGL readback is not Kodi GUI, Mali or HDMI/device acceptance.
"""
import argparse
import ctypes as C
import os
from pathlib import Path
import runpy
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SHADERS = ROOT / 'system/shaders/GLES/2.0'
base = runpy.run_path(str(ROOT / 'tools/test-bitmap-subtitle-colour-shader.py'))
I, U, F = base['I'], base['U'], base['F']
NAMES = ('default', 'texture', 'multi', 'multi_blendcolor', 'fonts',
         'texture_noalpha', 'texture_noblend')
VERTEX = '''#version 100
attribute vec2 position;
uniform lowp vec4 colour;
varying vec4 m_cord0;
varying vec4 m_cord1;
varying lowp vec4 m_colour;
void main(){gl_Position=vec4(position,0.0,1.0);
m_cord0=m_cord1=vec4(0.5,0.5,0.0,1.0);m_colour=colour;}
'''


class GUI(base['GLES']):
    def __init__(self):
        super().__init__()
        for name, args in [('glUniform4f', [I, F, F, F, F]), ('glActiveTexture', [U])]:
            self.fn[name] = C.CFUNCTYPE(None, *args)(self.proc(name.encode()))
        self.second = U()
        self.glGenTextures(1, C.byref(self.second))
        self.glActiveTexture(0x84C1)
        self.glBindTexture(0x0DE1, self.second)
        self.glTexParameteri(0x0DE1, 0x2800, 0x2600)
        self.glTexParameteri(0x0DE1, 0x2801, 0x2600)
        self.glActiveTexture(0x84C0)

    def program(self, fragment, defines='', helper=None, vertex=VERTEX):
        # Same insertion point and define placement as the production loader.
        fragment = fragment.replace('#version 100', '#version 100\n' + defines, 1)
        if helper is not None:
            fragment = fragment.replace('void main', helper + '\nvoid main', 1)
        vs = self.shader(vertex, 0x8B31)
        fs = self.shader(fragment, 0x8B30)
        program = self.glCreateProgram()
        self.glAttachShader(program, vs)
        self.glAttachShader(program, fs)
        self.glLinkProgram(program)
        ok = I()
        self.glGetProgramiv(program, 0x8B82, C.byref(ok))
        log = C.create_string_buffer(16384)
        self.glGetProgramInfoLog(program, len(log), None, log)
        if not ok.value:
            raise RuntimeError(log.value.decode())
        self.glDeleteShader(vs)
        self.glDeleteShader(fs)
        self.programs.append(program)
        return program

    def pixel(self, program, texel, colour, second, white=1, saturation=1, gate=1,
              bitmap_brightness=1, bitmap_saturation=1, bitmap_peak=1, pma=0):
        self.glUseProgram(program)
        for name, value in [('m_guiTuning', gate), ('m_guiPeak', white),
                            ('m_guiSaturation', saturation), ('m_sdrPeak', .75),
                            ('m_sdrBrightness', bitmap_brightness),
                            ('m_sdrSaturation', bitmap_saturation),
                            ('m_subtitlePeak', bitmap_peak), ('m_pma', pma),
                            ('m_brightness', 0), ('m_contrast', 1),
                            ('m_pqRefNits', 203), ('m_pqSaturation', 1.25),
                            ('m_pqTonemap', 0), ('m_pqMode', 1)]:
            self.glUniform1f(self.glGetUniformLocation(program, name.encode()), value)
        for name in (b'm_unicol', b'colour'):
            self.glUniform4f(self.glGetUniformLocation(program, name), *colour)
        for unit, texture, rgba in [(0, self.texture, texel), (1, self.second, second)]:
            self.glActiveTexture(0x84C0 + unit)
            self.glBindTexture(0x0DE1, texture)
            data = (C.c_ubyte * 4)(*rgba)
            self.glTexImage2D(0x0DE1, 0, 0x1908, 1, 1, 0, 0x1908, 0x1401, data)
            self.glUniform1i(self.glGetUniformLocation(program, f'm_samp{unit}'.encode()), unit)
        self.glActiveTexture(0x84C0)
        position = self.glGetAttribLocation(program, b'position')
        self.glVertexAttribPointer(position, 2, 0x1406, 0, 0, self.vertices)
        self.glEnableVertexAttribArray(position)
        self.glDrawArrays(0x0005, 0, 4)
        output = (C.c_ubyte * 4)()
        self.glReadPixels(0, 0, 1, 1, 0x1908, 0x1401, output)
        assert self.glGetError() == 0
        return tuple(output)


def authored(name, texel, colour, second):
    texture = [x / 255 for x in texel]
    other = [x / 255 for x in second]
    if name == 'default':
        return list(colour)
    if name == 'texture':
        return [x * y for x, y in zip(texture, colour)]
    if name.startswith('multi'):
        rgb = [x * y for x, y in zip(texture, other)]
        return [x * y for x, y in zip(rgb, colour)] if name == 'multi_blendcolor' else rgb
    if name == 'fonts':
        return list(colour[:3]) + [colour[3] * texture[3]]
    if name == 'texture_noalpha':
        return texture[:3] + [1]
    return texture


def expected(rgba, white, saturation, variant):
    # Independent linear-light luminance/mix, then legacy range/PQ transforms.
    rgb = list(rgba[:3])
    if saturation != 1:
        linear = [x ** 2.2 for x in rgb]
        luminance = sum(x * y for x, y in zip(linear, (.2126, .7152, .0722)))
        rgb = [min(1, max(0, luminance + (x - luminance) * saturation)) ** (1 / 2.2)
               for x in linear]
    rgb = [x * white for x in rgb]
    if variant & 1:
        rgb = [x * 219 / 255 + 16 / 255 for x in rgb]
    if variant & 2:
        rgb = [x * .75 for x in rgb]
    return tuple(round(x * 255) for x in rgb) + (round(rgba[3] * 255),)


def verify(gl, helper, fragments, baseline, full=True):
    checks = 0
    for name in NAMES:
        fragment = fragments[name]
        for variant in range(4):
            defines = ('#define KODI_LIMITED_RANGE 1\n' if variant & 1 else '') + (
                '#define KODI_TRANSFER_PQ 1\n' if variant & 2 else '')
            program = gl.program(fragment, defines, helper)
            tuned = gl.program(fragment, defines + '#define KODI_GUI_COLOUR 1\n', helper)
            old = gl.program(baseline[name], defines)
            if full:
                for vertex in ('gles_shader.vert', 'gles_shader_clip.vert'):
                    gl.program(fragment, defines + '#define KODI_GUI_COLOUR 1\n', helper,
                               (SHADERS / vertex).read_text())
            for alpha in (0, 1, 32, 128, 255):
                for rgb in ((0, 0, 0), (10, 200, 40), (240, 60, 30), (127, 127, 127), (255, 255, 255)):
                    texel = rgb + (alpha,)
                    colour = (.71, .39, .17, alpha / 255)
                    second = (190, 240, 215, 193)
                    args = texel, colour, second
                    original = gl.pixel(old, *args)
                    neutral = gl.pixel(program, *args)
                    assert neutral == original, (name, 'neutral', variant, args, neutral, original)
                    for white, sat in ((0, 1), (.25 ** (1 / 2.2), 1), (1, 0), (1, .5), (1, 2), (.5, 2)):
                        got = gl.pixel(tuned, *args, white=white, saturation=sat)
                        want = expected(authored(name, *args), white, sat, variant)
                        assert all(abs(x - y) <= 2 for x, y in zip(got[:3], want[:3])), (name, variant, white, sat, got, want)
                        assert got[3] == original[3], (name, 'alpha', got, original)
                        checks += 1
    # Shared bitmap/PQ paths still use their existing independent controls when
    # the production overlay draw turns GUI tuning off. Include PMA/edge alphas.
    for variant in range(5):
        defines = ('#define KODI_PQ_TO_SDR 1\n' if variant == 4 else
                   ('#define KODI_LIMITED_RANGE 1\n' if variant & 1 else '') +
                   ('#define KODI_TRANSFER_PQ 1\n' if variant & 2 else ''))
        program = gl.program(fragments['texture_noblend'], defines + '#define KODI_GUI_COLOUR 1\n', helper)
        old = gl.program(baseline['texture_noblend'], defines)
        for alpha in (0, 1, 32, 128, 255):
            for rgb in ((30, 90, 120), (160, 40, 20), (255, 255, 255)):
                args = rgb + (alpha,), (1, 1, 1, 1), (255, 255, 255, 255)
                for brightness, sat, peak in ((1, 1, 1), (.5, 0, .65), (2, 2, .3)):
                    params = dict(bitmap_brightness=brightness, bitmap_saturation=sat, bitmap_peak=peak, pma=1)
                    expected_pixel = gl.pixel(old, *args, **params)
                    assert gl.pixel(program, *args, white=.4, saturation=0, gate=0, **params) == expected_pixel
                    # Corrected-PQ conversion is excluded even if a caller enables GUI tuning.
                    if variant == 4:
                        assert gl.pixel(program, *args, white=.4, saturation=0, gate=1, **params) == expected_pixel
                    checks += 1
    # Shared loader insertion/defines must leave dedicated text subtitles and
    # video RGBA draws unchanged even while GUI tuning is nonneutral.
    for name in ('fonts_subtitle', 'rgba'):
        fragment = (SHADERS / f'gles_shader_{name}.frag').read_text()
        for variant in range(4):
            defines = ('#define KODI_LIMITED_RANGE 1\n' if variant & 1 else '') + (
                '#define KODI_TRANSFER_PQ 1\n' if variant & 2 else '')
            old = gl.program(fragment, defines)
            program = gl.program(fragment, defines + '#define KODI_GUI_COLOUR 1\n', helper)
            for alpha in (0, 1, 32, 128, 255):
                for rgb in ((0, 0, 0), (10, 200, 40), (240, 60, 30), (127, 127, 127), (255, 255, 255)):
                    params = rgb + (alpha,), (.71, .39, .17, alpha / 255), (255, 255, 255, 255)
                    assert gl.pixel(program, *params, white=.4, saturation=0) == gl.pixel(old, *params)
                    checks += 1
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='58aafcddf7ec471961bac4aa7405e9a99e4296a7')
    parser.add_argument('--mutations', action='store_true')
    args = parser.parse_args()
    helper = (SHADERS / 'gui_colour.frag').read_text()
    fragments = {name: (SHADERS / f'gles_shader_{name}.frag').read_text() for name in NAMES}
    baseline = {name: subprocess.check_output(['git', 'show', f'{args.baseline_ref}:system/shaders/GLES/2.0/gles_shader_{name}.frag'], cwd=ROOT, text=True) for name in NAMES}
    # Dedicated subtitle fonts, video and the final PGS/GUI compositor remain byte-identical.
    for path in ('system/shaders/GLES/2.0/gles_shader_fonts_subtitle.frag',
                 'system/shaders/GLES/2.0/gles_shader_rgba.frag',
                 'xbmc/rendering/gles/GuiCompositeShaderGLES.cpp'):
        assert (ROOT / path).read_text() == subprocess.check_output(['git', 'show', f'{args.baseline_ref}:{path}'], cwd=ROOT, text=True), path
    os.environ.setdefault('EGL_PLATFORM', 'surfaceless')
    os.environ.setdefault('LIBGL_ALWAYS_SOFTWARE', '1')
    os.environ.setdefault('MESA_SHADER_CACHE_DISABLE', 'true')
    gl = GUI()
    try:
        count = verify(gl, helper, fragments, baseline)
        print(f'PASS: {count} real GLES GUI/overlay pixel cases, seven fragments/four variants; '
              'both production vertices link, parent neutral bytes/alpha unchanged')
        if args.mutations:
            mutations = {
                'ignore-white': ('return rgb * m_guiPeak;', 'return rgb;'),
                'encoded-luma': ('pow(max(rgb, vec3(0.0)), vec3(2.2))', 'max(rgb, vec3(0.0))'),
                'wrong-luma': ('vec3(0.2126, 0.7152, 0.0722)', 'vec3(0.3333)'),
                'ignore-gate': ('m_guiTuning < 0.5 || ', ''),
            }
            for name, (old, new) in mutations.items():
                assert old in helper
                try:
                    verify(gl, helper.replace(old, new), fragments, baseline, False)
                except AssertionError:
                    print('REJECTED:', name)
                else:
                    raise AssertionError('survived mutation: ' + name)
            for name, mutate in (
                ('change-alpha', lambda f: f.replace('rgb.rgb = guiColour(rgb.rgb);', 'rgb.rgb = guiColour(rgb.rgb); rgb.a *= m_guiPeak;')),
                ('wrong-transfer-order', lambda f: f.replace(
                    '#if defined(KODI_GUI_COLOUR)\n  rgb.rgb = guiColour(rgb.rgb);\n#endif\n', '').replace(
                    '  gl_FragColor = rgb;', '#if defined(KODI_GUI_COLOUR)\n  rgb.rgb = guiColour(rgb.rgb);\n#endif\n  gl_FragColor = rgb;')),
            ):
                changed = dict(fragments)
                changed['texture'] = mutate(changed['texture'])
                assert changed['texture'] != fragments['texture']
                try:
                    verify(gl, helper, changed, baseline, False)
                except AssertionError:
                    print('REJECTED:', name)
                else:
                    raise AssertionError('survived mutation: ' + name)
    finally:
        gl.close()


if __name__ == '__main__':
    main()
