#!/usr/bin/env python3
"""Decode the native CE capability text and exercise Smart's independent target.

Host checks use production headers, including snapshot acceptance. They do not
measure a TV's luminance or verify a physical HDMI transition.
"""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CODE = r'''
#include "windowing/amlogic/AMLHDRCapabilities.h"
#include <cassert>
#include <map>
#include <iostream>
using namespace AML::DisplayPeak;
std::string dv(const std::string& version,const std::string& field){
 return "DolbyVision RX support list:\nVSVDB Version: V"+version+"\n"+field+"\n";}
std::string hdr(const std::string& maximum){return
 "HDR10Plus Supported: 0\nHDR Static Metadata:\nSupported EOTF:\n"
 "Traditional HDR: 1\nSMPTE ST 2084: 1\nHybrid Log-Gamma: 0\n"
 "Supported SMD type1: 1\nLuminance Data\nMax: "+maximum+
 "\nAvg: 0\nMin: 0\nHDR Dynamic Metadata:\n";}
int main(){
 // CE prints a quantized index for V1/V2, full PQ code for V0.
 assert(DolbyVision(dv("1(12-byte)","TmaxM: 0nti"))==100);
 assert(DolbyVision(dv("1(15-byte)","TmaxM: 127nti"))==6450);
 assert(DolbyVision(dv("2","TmaxQ: 0pqi"))==96);
 assert(DolbyVision(dv("2","TmaxQ: 15pqi"))==934);
 assert(DolbyVision(dv("2","TmaxQ: 31pqi"))==10000);
 assert(DolbyVision(dv("0","TmaxQ: 3079pqi"))==1001);
 assert(DolbyVision(dv("0","TmaxQ: 4095pqi"))==10000);
 assert(DolbyVision(dv("0","TmaxQ: 0pqi"))==0);
 for(const auto& text:{dv("3","TmaxQ: 15pqi"),dv("2","TmaxQ: 32pqi"),
   dv("1(13-byte)","TmaxM: 1nti"),dv("1(12-byte)","TmaxM: 128nti"),
   dv("2","TmaxQ: -1pqi"),dv("2","TmaxQ: 15garbage"),
   dv("2","TmaxQ: 15pqi\nTmaxQ: 16pqi"),dv("2",""),
   std::string("The Rx don't support DolbyVision\n"),
   dv("2","TmaxQ: 15pqi\nVSVDB Version: V2")}) assert(DolbyVision(text)==0);
 assert(HDR(hdr("128"))==800);
 assert(HDR(hdr("129"))==818); // Preserve fractional CTA exponent.
 assert(HDR(hdr("255"))==12526);
 for(const auto& value:{"0","256","-1","abc","128junk","128\nMax: 100"})
  assert(HDR(hdr(value))==0);
 assert(HDR("mask rx hdr capability")==0);
 auto noPQ=hdr("128");noPQ.replace(noPQ.find("2084: 1"),7,"2084: 0");assert(HDR(noPQ)==0);
 auto noType=hdr("128");noType.replace(noType.find("type1: 1"),8,"type1: 0");assert(HDR(noType)==0);
 assert(HDR(hdr("0")+"CUVA supported: 1\ndisplay_maximum_luminance: 4000\n")==0);
 assert(Reported(hdr("128"),dv("2","TmaxQ: 15pqi"))==934);
 assert(Reported(hdr("128"),dv("2","TmaxQ: 32pqi"))==800);
 Publish(934);assert(Resolve(false,500)==934);assert(Resolve(true,500)==500);
 Publish(0);assert(Resolve(false,500)==1000);assert(Resolve(true,500)==500);
 // The actual snapshot parser must reject a changing capability before publishing.
 std::map<std::string,std::string> values{{"hpd_state","1"},{"edid_parsing","ok"},
  {"rawedid","00ffffffffffff00"+std::string(240,'0')},
  {"hdr_cap",hdr("128")},{"dv_cap",dv("2","TmaxQ: 15pqi")}};
 int peak=0;auto caps=AML::HDR::ReadStable([&](const char* field){
   return std::optional<std::string>(values.at(field));},nullptr,&peak);
 assert(caps&&peak==934);
 int reads=0;peak=123;
 caps=AML::HDR::ReadStable([&](const char* field){
  ++reads;return std::optional<std::string>(reads==10?dv("2","TmaxQ: 20pqi"):values.at(field));},nullptr,&peak);
 assert(!caps&&peak==123);
 std::cout<<"Native DV/HDR peak decoding, override precedence and stable snapshot checks passed\n";
}
'''
with tempfile.TemporaryDirectory(prefix='aml-display-peak-') as temporary:
    out = Path(temporary)
    cpp = out / 'test.cpp'
    cpp.write_text(CODE)
    executable = out / 'test'
    subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                    '-Werror', '-Wdouble-promotion', '-fsanitize=address,undefined',
                    '-fno-pie', '-no-pie', '-I', str(ROOT / 'xbmc'), str(cpp),
                    '-o', str(executable)], check=True)
    environment = os.environ.copy()
    environment['ASAN_OPTIONS'] = environment.get('ASAN_OPTIONS', '') + ':detect_leaks=0'
    subprocess.run([str(executable)], env=environment, check=True)
