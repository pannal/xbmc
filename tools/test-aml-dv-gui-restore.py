#!/usr/bin/env python3
"""Check the Always-On Dolby Vision GUI restore after playback and its codec-pending guard.

Extracted Kodi functions run against stubs; this proves Kodi policy only, not device HDMI output.
"""
import argparse
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

GUI_RESTORE = r'''
#include <cassert>
#include <iostream>
#include <mutex>
enum {LOGDEBUG,LOGINFO};
struct CLog {template<typename... A> static void Log(int, const char*, A&&...){}};
struct CDVCoreGuard {explicit CDVCoreGuard(const char*){}};
enum {DV_MODE_ON,DV_MODE_ON_DEMAND,DV_MODE_OFF};
#define DOLBY_VISION_OUTPUT_MODE_IPT (unsigned int)(0)
#define DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL (unsigned int)(1)
static std::mutex s_dvStartMutex;
int kodiMode=DV_MODE_ON;bool playing=false,dvOn=false;unsigned coreMode=5;int starts=0;
static int s_dvCodecsPending = 0;
int aml_dv_mode(){return kodiMode;}
bool aml_dv_playback_active(){return playing;}
bool aml_is_dv_enable(){return dvOn;}
unsigned int aml_dv_dolby_vision_mode(){return coreMode;}
const char* aml_dv_output_mode_to_string(unsigned){return "";}
void _dv_start_locked(){starts++;}
@METHOD@
@PENDING@
int main(){
 for(int m:{DV_MODE_ON,DV_MODE_ON_DEMAND,DV_MODE_OFF})for(bool p:{false,true})for(bool e:{false,true})for(unsigned c:{0u,1u,2u,5u})for(int q:{0,1}){
  kodiMode=m;playing=p;dvOn=e;coreMode=c;starts=0;s_dvCodecsPending=q;
  const bool restore=m==DV_MODE_ON&&!p&&!q;
  const bool switched=aml_dv_restore_gui_ipt("t");
  assert(starts==(restore?1:0));
  assert(switched==(restore&&(!e||c>1u)));
 }
 std::cout<<"PASS: always-on GUI restore re-enables DV after bypassed (DV-disabled) playback; skips during playback, codec-pending or other modes\n";
 // Stream change: B opens before A's decoder starts; one release must not unblock restores.
 kodiMode=DV_MODE_ON;playing=false;dvOn=false;s_dvCodecsPending=0;
 aml_dv_codec_pending(true);aml_dv_codec_pending(true);aml_dv_codec_pending(false);
 starts=0;aml_dv_restore_gui_ipt("t");assert(starts==0);
 aml_dv_codec_pending(false);aml_dv_restore_gui_ipt("t");assert(starts==1);
 aml_dv_codec_pending(false);assert(s_dvCodecsPending==0);
 std::cout<<"PASS: GUI restore waits for every opened codec's decoder start; release never underflows\n";
}
'''


def gui_restore_code():
    source = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    return GUI_RESTORE.replace('@METHOD@', function(source, 'bool aml_dv_restore_gui_ipt(const char* reason)')).replace(
        '@PENDING@', function(source, 'void aml_dv_codec_pending(bool pending)'))


def run(code, negative=False):
    with tempfile.TemporaryDirectory(prefix='dv-gui-restore-') as directory:
        path = Path(directory)
        (path / 'test.cpp').write_text(code)
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        str(path / 'test.cpp'), '-o', str(path / 'test')], check=True)
        result = subprocess.run([str(path / 'test')], text=True, capture_output=True, timeout=20)
        if negative:
            assert result.returncode and 'Assertion' in result.stderr, result.stdout + result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())


def codec_check(source):
    open_ = function(source, 'bool CDVDVideoCodecAmlogic::Open(CDVDStreamInfo &hints, CDVDCodecOptions &options)\n{')
    tail = open_[open_.rindex('m_has_keyframe = false;'):open_.index('return true;\nFAIL:')]
    assert re.search(r'if \(!m_dvCodecPending\)\s*\{\s*aml_dv_codec_pending\(true\);\s*m_dvCodecPending = true;\s*\}', tail), \
        'successful open marks a codec pending'
    release = re.compile(r'if \(m_dvCodecPending\)\s*\{\s*aml_dv_codec_pending\(false\);\s*m_dvCodecPending = false;\s*\}')
    close = function(source, 'void CDVDVideoCodecAmlogic::Close(void)\n{')
    assert release.search(close) and release.search(close).start() < close.index('m_Codec->CloseDecoder('), \
        'close releases pending before teardown'
    add = function(source, 'bool CDVDVideoCodecAmlogic::AddData(const DemuxPacket &packet)\n{')
    started = add[add.index('m_Codec->OpenDecoder()'):]
    started = started[started.index('m_opened = true;'):]
    assert release.match(started[len('m_opened = true;'):].lstrip()), 'decoder start releases pending'
    print('PASS: codec holds GUI restores pending from open to decoder start or close')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    codec = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text()
    restore = gui_restore_code()
    run(restore)
    codec_check(codec)
    if not args.negative_controls:
        return
    for label, old, new in [
        ('GUI restore skips DV-disabled', '(aml_dv_mode() != DV_MODE_ON || aml_dv_playback_active()',
         '(aml_dv_mode() != DV_MODE_ON || !aml_is_dv_enable() || aml_dv_playback_active()'),
        ('GUI restore during playback', ' || aml_dv_playback_active() ||', ' ||'),
        ('GUI restore while codec pending', ' || s_dvCodecsPending > 0)', ')'),
        ('pending release underflows', '  else if (s_dvCodecsPending > 0)\n', '  else\n'),
        ('pending not counted', '    ++s_dvCodecsPending;', '    s_dvCodecsPending = 1;'),
        ('DV-disabled not reported as switch', 'needs_mode_switch = !dv_enabled ||', 'needs_mode_switch = '),
    ]:
        assert old in restore, label
        run(restore.replace(old, new), negative=True)
        print('REJECTED:', label)
    for label, old, new in [
        ('open not pending', '    aml_dv_codec_pending(true);\n', '\n'),
        ('decoder start keeps pending', '      m_opened = true;\n      if (m_dvCodecPending)', '      m_opened = true;\n      if (false)'),
        ('close keeps pending', '  CLog::Log(LOGINFO, "{}::{}", __MODULE_NAME__, __FUNCTION__);\n\n  if (m_dvCodecPending)',
         '  CLog::Log(LOGINFO, "{}::{}", __MODULE_NAME__, __FUNCTION__);\n\n  if (false)'),
    ]:
        assert old in codec, label
        try:
            codec_check(codec.replace(old, new, 1))
        except AssertionError:
            print('REJECTED:', label)
        else:
            raise AssertionError('mutant passed: ' + label)


if __name__ == '__main__':
    main()