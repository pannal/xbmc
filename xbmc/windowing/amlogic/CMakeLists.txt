set(SOURCES AMLDPMSSupport.cpp
            WinSystemAmlogic.cpp
            VideoSyncAML.cpp
            DolbyVisionAML.cpp)

set(HEADERS AMLDPMSSupport.h
            AMLDeferredWork.h
            AMLNativeTransaction.h
            WinSystemAmlogic.h
            VideoSyncAML.h
            DolbyVisionAML.h)

if(OPENGLES_FOUND)
  list(APPEND SOURCES WinSystemAmlogicGLESContext.cpp)
  list(APPEND HEADERS WinSystemAmlogicGLESContext.h)
endif()

core_add_library(windowing_Amlogic)
