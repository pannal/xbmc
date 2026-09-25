set(SOURCES DVDAudioCodecFFmpeg.cpp
            DVDAudioCodecPassthrough.cpp)

set(HEADERS DVDAudioCodec.h
            DVDAudioCodecFFmpeg.h
            DVDAudioCodecPassthrough.h)

if(CORE_SYSTEM_NAME STREQUAL android)
  list(APPEND SOURCES DVDAudioCodecAndroidMediaCodec.cpp)
  list(APPEND HEADERS DVDAudioCodecAndroidMediaCodec.h)
endif()

# The binaural codec forks its renderer and talks to it over pipes, which is
# POSIX, and the helper it runs ships for Linux only. Elsewhere it is not built
# and the factory keeps the ordinary decoders; see DVDFactoryCodec.cpp.
if(CORE_SYSTEM_NAME STREQUAL linux)
  list(APPEND SOURCES DVDAudioCodecOmniphony.cpp
                      OmniphonyPcmSource.cpp
                      OmniphonyPcmStream.cpp
                      OmniphonyRateCheck.cpp
                      OmniphonyTimeline.cpp)
  list(APPEND HEADERS DVDAudioCodecOmniphony.h
                      OmniphonyPcmSource.h
                      OmniphonyPcmStream.h
                      OmniphonyTimeline.h)
endif()

core_add_library(dvdaudiocodecs)
