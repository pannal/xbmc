set(SOURCES PerformanceCores.cpp
            Event.cpp
            Thread.cpp
            Timer.cpp)

set(HEADERS PerformanceCores.h
            Condition.h
            CriticalSection.h
            Event.h
            Lockables.h
            SharedSection.h
            SingleLock.h
            SystemClock.h
            Thread.h
            Timer.h
            IThreadImpl.h
            IRunnable.h)

core_add_library(threads)
