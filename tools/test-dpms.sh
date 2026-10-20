#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Executes production power-handling methods with recording application services.
# No real renderer, HDMI sink, kernel, CEC adapter, or audio engine is simulated.
set -Eeuo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
cp "$root/tools/dpms-test.cpp" "$out/test.cpp"
awk '/^(bool|void) CApplicationPowerHandling::(ToggleDPMS|WakeDPMS|ProcessDPMS|ResetDPMS|ReleaseDPMS|ReleaseAutomaticDPMS|WakeUpScreenSaverAndDPMS|AnnounceScreenSaverDeactivated|CheckScreenSaverAndDPMS|PlaybackPreventsDPMS|ActivateScreenSaver|CheckOSScreenSaverInhibitionSetting)\(/ {copy=1} copy {print} copy && /^}/ {copy=0}' \
  "$root/xbmc/application/ApplicationPowerHandling.cpp" >> "$out/test.cpp"
awk '/^bool CWinSystemAmlogicGLESContext::PrepareForShutdown\(/ {copy=1} copy {print} copy && /^}$/ {copy=0}' \
  "$root/xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp" >> "$out/test.cpp"
mkdir -p "$out/stubs/utils"
cat > "$out/stubs/utils/log.h" <<'LOG'
#pragma once
constexpr int LOGERROR = 1;
constexpr int LOGDEBUG = 2;
struct CLog { template<class... T> static void Log(T&&...) {} };
LOG
"${CXX:-g++}" -std=c++17 -Wall -Wextra -Werror -pthread \
  -fsanitize=address,undefined -fno-omit-frame-pointer -fno-pie -no-pie \
  -I"$out/stubs" -I"$root/xbmc" "$out/test.cpp" \
  "$root/xbmc/windowing/amlogic/AMLDPMSSupport.cpp" -o "$out/test"
if [ "${1:-}" = --case ]; then
  "$out/test" "$2"
  exit
fi
"$out/test"
echo 'DPMS application, queue, and Amlogic adapter tests passed.'
if [ "${1:-}" = --negative-controls ]; then
  for mutation in render-before-wake wait-for-reset ignore-failure playing-without-owner \
                  ignore-pending-open skip-playback-recheck unstable-video-classification \
                  ignore-activation-playback ignore-activation-pending \
                  ignore-audio-capability ignore-persistent-capability; do
    case "$mutation" in
      render-before-wake) sed 's/: dpms->DisablePowerSaving());/: (SetRenderGUI(true), dpms->DisablePowerSaving()));/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      wait-for-reset) sed 's/request->active \&\& !dpms->CanEnablePowerSaving()/!dpms->CanEnablePowerSaving()/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      ignore-failure) sed 's/Complete(\*request, success)/Complete(*request, true)/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      playing-without-owner) sed 's/appPlayer->HasPlayer()/appPlayer->IsPlaying()/g' "$out/test.cpp" > "$out/mutant.cpp" ;;
      ignore-pending-open) sed 's/return appPlayer->HasPendingOpen() ||/return/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      skip-playback-recheck) sed 's/request->active \&\& !request->explicitRequest/false \&\& request->active \&\& !request->explicitRequest/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      unstable-video-classification) sed 's/(!IsPythonScreenSaverActive() || m_screensaverStartedWithPlayback ||/(false ||/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      ignore-activation-playback) sed 's/appPlayer \&\& (appPlayer->HasPlayer() || appPlayer->HasPendingOpen());/false;/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      ignore-activation-pending) sed 's/appPlayer->HasPlayer() || appPlayer->HasPendingOpen()/appPlayer->HasPlayer()/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      ignore-audio-capability) sed 's/dpms \&\& dpms->PowerSavingInterruptsAudio() \&\& m_dpmsControl.Wake()/dpms \&\& m_dpmsControl.Wake()/' "$out/test.cpp" > "$out/mutant.cpp" ;;
      ignore-persistent-capability) sed 's/!dpms || !dpms->HasPersistentPowerSaving()/!dpms/' "$out/test.cpp" > "$out/mutant.cpp" ;;
    esac
    "${CXX:-g++}" -std=c++17 -Wall -Wextra -Werror -pthread -I"$out/stubs" -I"$root/xbmc" \
      "$out/mutant.cpp" "$root/xbmc/windowing/amlogic/AMLDPMSSupport.cpp" -o "$out/mutant"
    if (ulimit -c 0; "$out/mutant") > "$out/mutant.log" 2>&1; then
      echo "Negative control unexpectedly passed: $mutation" >&2; exit 1
    fi
    grep -q 'Assertion' "$out/mutant.log"
    echo "Rejected negative control: $mutation"
  done
fi
