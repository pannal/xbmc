// SPDX-License-Identifier: GPL-2.0-or-later
struct Harness
{
  State& state;
  CJobManager jobs;
  CGUILargeTextureManager images;
  explicit Harness(State& s) : state(s)
  {
    activeJobs = &jobs;
    currentState = &s;
    frameTime = 0;
  }
  ~Harness()
  {
    while (!images.m_queued.empty())
    {
      const auto path = images.m_queued.front().second->GetPath();
      images.ReleaseImage(path, true);
    }
    while (!images.m_allocated.empty())
    {
      auto* image = images.m_allocated.front();
      const auto path = image->GetPath();
      if (image->m_refCount)
        images.ReleaseImage(path, true);
      else
        images.CleanupUnusedImages(true);
    }
    activeJobs = nullptr;
    currentState = nullptr;
  }
  bool Work(CJob* job)
  {
    currentState = &state;
    bool ok = false;
    try
    {
      ok = job->DoWork();
    }
    catch (const std::exception&)
    {
    }
    jobs.OnJobComplete(ok, job);
    return ok;
  }
  unsigned New(const std::string& path, bool background = false)
  {
    CTextureArray array;
    Check(images.GetImage(path, array, true, true, background), "new image request");
    for (const auto& q : images.m_queued)
      if (q.second->GetPath() == path)
        return q.first;
    throw std::runtime_error("missing decode entry");
  }
  void Decode(const std::string& path)
  {
    const auto id = New(path);
    Check(id && Work(jobs.Start(id)), "decode succeeds");
  }
  void Request(const std::string& path, bool background = false, bool prefetch = false)
  {
    images.RequestImageUpload(path, background, prefetch, frameTime);
  }
  bool Active()
  {
    Check(images.m_activeUploadJobID != 0, "one active job is available");
    return Work(jobs.Start(images.m_activeUploadJobID));
  }
  bool Published(const std::string& path)
  {
    CTextureArray a;
    return images.GetImage(path, a, false) && a.size();
  }
};

static void PriorityAndSerialization()
{
  State s;
  Harness h(s);
  for (auto path : {"a", "prefetch", "fanart", "poster"})
    h.Decode(path);
  h.Request("a");
  h.Request("prefetch", false, true);
  h.Request("fanart", true);
  h.Request("poster");
  Check(h.jobs.added[CJob::PRIORITY_HIGH] == 1, "waiters never occupy decoder workers");
  Check(h.images.m_uploadQueue.size() == 3, "waiting images remain in manager queue");
  Check(!h.Published("a") && !h.Published("poster"), "no publication before completion");
  h.Active();
  Check(h.images.m_allocatedLookup.at("poster")->m_uploadJobID == h.images.m_activeUploadJobID,
        "visible poster precedes fanart and speculation");
  h.Active();
  Check(h.images.m_allocatedLookup.at("fanart")->m_uploadJobID == h.images.m_activeUploadJobID,
        "fanart precedes speculation");
  h.Active();
  h.Active();
  Check(s.uploads == 4 && s.releases == 4 && s.leases == 0,
        "each image uploaded once and released before publication");
  Check(h.images.m_activeUploadJobID == 0 && h.images.m_uploadQueue.empty(), "queue drains");
  ++cases;
}

static void Speculation()
{
  State s;
  Harness h(s);
  for (unsigned i = 0; i < 20; i++)
    h.Decode(std::to_string(i));
  for (unsigned i = 0; i < 20; i++)
    h.Request(std::to_string(i), false, true);
  Check(h.images.m_uploadQueue.size() == 8 && !h.images.m_activeUploadJobID,
        "bounded speculative queue waits for control processing");
  frameTime = 251;
  h.images.ProcessDeferred();
  Check(h.images.m_uploadQueue.empty() && !h.images.m_activeUploadJobID,
        "stale unseen requests expire without GPU work");
  h.Request("19", false, true);
  h.images.ProcessDeferred();
  Check(h.images.m_activeUploadJobID != 0, "a fresh nearby request is admitted");
  h.Request("18");
  Check(h.jobs.added[CJob::PRIORITY_HIGH] == 1, "promotion cannot spawn a parallel waiter");
  h.Active();
  Check(h.images.m_allocatedLookup.at("18")->m_uploadJobID == h.images.m_activeUploadJobID,
        "visible item bypasses speculative limit");
  h.Active();
  Check(s.uploads == 2, "unused speculative images remain decoded only");
  ++cases;
}

static void ReleaseAndReacquire()
{
  State s;
  Harness h(s);
  h.Decode("a");
  h.Decode("b");
  h.Request("a");
  h.Request("b");
  h.images.ReleaseImage("b");
  Check(h.images.m_uploadQueue.empty(), "zero consumers discard waiting upload immediately");
  h.Active();
  Check(s.uploads == 1 && !h.images.m_activeUploadJobID, "unused cached entry does not upload");
  CTextureArray a;
  Check(h.images.GetImage("b", a, true) && !a.size(), "decoded entry survives grace period");
  h.Request("b");
  h.Active();
  Check(s.decodes == 2 && s.uploads == 2 && h.Published("b"),
        "reacquire reuses decode and uploads once");
  h.images.ReleaseImage("a");
  Check(h.images.GetImage("a", a, true) && a.size(), "completed cached image stays reusable");
  ++cases;
}

static void RemovedActiveEntry()
{
  State s;
  Harness h(s);
  h.Decode("a");
  h.Decode("b");
  h.Request("a");
  h.Request("b");
  const auto old = h.images.m_activeUploadJobID;
  h.images.ReleaseImage("a", true);
  Check(h.images.m_activeUploadJobID == old, "deletion does not lose the completion reservation");
  Check(!h.Active() && s.uploads == 0, "lifetime token cancels an admitted but unstarted image");
  Check(h.images.m_activeUploadJobID && h.images.m_activeUploadJobID != old,
        "completion advances after deletion");
  h.Active();
  Check(h.Published("b") && s.uploads == 1, "next visible image is not stranded");
  ++cases;
}

static void ReplacedPath()
{
  State s;
  Harness h(s);
  h.Decode("a");
  h.Request("a");
  const auto old = h.images.m_activeUploadJobID;
  h.images.ReleaseImage("a", true);
  h.Decode("a");
  h.Request("a");
  Check(!h.Active(), "old entry cancelled");
  const auto next = h.images.m_activeUploadJobID;
  h.images.OnJobComplete(old, true, nullptr);
  Check(next && h.images.m_activeUploadJobID == next && !h.Published("a"),
        "old copied callback cannot publish or release a replacement job");
  h.Active();
  Check(s.decodes == 2 && s.uploads == 1 && h.Published("a"), "replacement has its own upload");
  ++cases;
}

static void RunningCancellation()
{
  State s;
  Harness h(s);
  h.Decode("a");
  h.Decode("b");
  h.Request("a");
  h.Request("b");
  s.blockAfterUpload = true;
  auto* job = h.jobs.Start(h.images.m_activeUploadJobID);
  std::exception_ptr failure;
  std::thread thread(
      [&]
      {
        try
        {
          h.Work(job);
        }
        catch (...)
        {
          failure = std::current_exception();
        }
      });
  s.uploadBarrier.Await();
  h.images.ReleaseImage("a", true);
  Check(h.images.m_activeUploadJobID != 0 && !h.Published("b"),
        "running job retains reservation and ownership");
  s.uploadBarrier.Release();
  thread.join();
  if (failure)
    std::rethrow_exception(failure);
  Check(!s.gpuDestroyedWithLock, "unpublished GPU data retires outside callback locks");
  s.blockAfterUpload = false;
  h.Active();
  Check(h.Published("b") && s.leases == 0, "next upload follows lease release");
  ++cases;
}

static void Fallbacks()
{
  for (int mode = 0; mode < 4; mode++)
  {
    State s;
    s.async = mode != 0;
    s.uploadUnsupported = mode == 2;
    s.throwUpload = mode == 3;
    Harness h(s);
    h.Decode("a");
    h.Request("a");
    if (mode == 0)
    {
      Check(h.Published("a") && !h.images.m_activeUploadJobID, "async off is unchanged");
    }
    else if (mode == 1)
    {
      h.images.ReleaseImage("a", true);
      h.Active();
      h.Decode("b");
      h.jobs.reject = true;
      h.Request("b");
      Check(h.Published("b") && !h.images.m_activeUploadJobID,
            "rejected upload preserves CPU fallback");
    }
    else if (mode == 2)
    {
      h.Active();
      Check(h.Published("a") && !s.uploads, "unavailable EGL context preserves CPU fallback");
    }
    else
    {
      Check(!h.Active() && !h.Published("a") && !s.gpuDestroyedWithLock,
            "partial upload error is never published or destroyed under callback locks");
    }
    ++cases;
  }
}

static void DemandDuringDecode()
{
  State s;
  Harness h(s);
  const auto id = h.New("a");
  h.Request("a", false, true);
  Check(!h.images.m_activeUploadJobID, "request can precede decode");
  h.Work(h.jobs.Start(id));
  Check(h.images.m_activeUploadJobID != 0, "decode completion joins upload scheduler");
  h.Active();
  Check(h.Published("a"), "early demand is not lost");
  ++cases;
}

static void DecodePlacement()
{
  State s;
  Harness h(s);
  const auto placementOf = [&](const std::string& path)
  {
    for (const auto& q : h.images.m_queued)
      if (q.second->GetPath() == path)
        return q.second->m_decodePlacement;
    for (auto* image : h.images.m_allocated)
      if (image->GetPath() == path)
        return image->m_decodePlacement;
    throw std::runtime_error("missing image");
  };
  // The decode job holds the image's own placement while it decodes; nothing
  // draws it yet, so the decode may start on the slow cluster.
  const auto a = h.New("a");
  const auto pa = placementOf("a");
  Check(pa && pa->scopes == 0, "a queued decode has not started yet");
  h.Work(h.jobs.Start(a));
  Check(pa->scopes == 1 && !pa->neededAtScope, "the decode runs with the image's placement");
  // Fanart and look-ahead requests do not need it now; a foreground draw does.
  const auto b = h.New("b", true);
  h.Request("b", true);
  Check(!placementOf("b")->neededNow, "a background request can wait");
  const auto c = h.New("c");
  h.Request("c", false, true);
  Check(!placementOf("c")->neededNow, "a look-ahead request can wait");
  h.Request("c");
  Check(placementOf("c")->neededNow, "a foreground draw needs it now");
  h.Work(h.jobs.Start(c));
  Check(placementOf("c")->neededAtScope, "a decode started after the draw is never restricted");
  // Demand during a running decode reaches the same object the worker holds.
  const auto d = h.New("d");
  const auto pd = placementOf("d");
  CJob* job = h.jobs.Start(d);
  h.Request("d");
  Check(pd->neededNow && pd->scopes == 0, "demand before the decode body runs");
  h.Work(job);
  Check(pd->scopes == 1 && pd->neededAtScope, "the running decode sees the demand");
  // Each image has its own placement; the unknown-geometry fallback counts as a draw.
  Check(placementOf("a") != placementOf("c"), "placements are per image");
  const auto e = h.New("e");
  h.images.RequestImageUpload("e");
  Check(placementOf("e")->neededNow, "default request counts as a foreground draw");
  if (b) // a deferred background decode has no job yet
    h.Work(h.jobs.Start(b));
  h.Work(h.jobs.Start(e));
  ++cases;
}

static void BackgroundHold()
{
  State s;
  Harness h(s);
  Check(h.New("a", true) == 0, "background decode is deferred");
  h.images.SetScrolling();
  h.images.ProcessDeferred();
  Check(h.jobs.Queued() == 0, "motion keeps decode deferred");
  h.images.ProcessDeferred();
  const auto id = h.jobs.First();
  Check(id != 0, "rest admits decode");
  h.Work(h.jobs.Start(id));
  h.Request("a", true);
  Check(!h.images.m_activeUploadJobID, "background upload remains held");
  h.Request("a");
  h.Active();
  Check(h.Published("a"), "foreground consumer bypasses background hold");
  h.images.ProcessDeferred();
  CTextureArray a;
  Check(h.images.GetImage("a", a, false, true, true) && a.size(),
        "background sees texture after motion release");
  ++cases;
}

static void FreshVisibleFirst()
{
  State s;
  Harness h(s);
  for (auto p : {"a", "old", "near", "visible"})
    h.Decode(p);
  h.Request("a");
  h.Request("old");
  frameTime = 16;
  h.Request("near", false, true);
  h.Request("visible");
  h.Active();
  Check(h.images.m_allocatedLookup.at("visible")->m_uploadJobID == h.images.m_activeUploadJobID,
        "current visible request precedes retained cache warmup");
  h.Active();
  Check(h.images.m_allocatedLookup.at("near")->m_uploadJobID == h.images.m_activeUploadJobID,
        "nearby work precedes stale visible cache work");
  h.Active();
  h.Active();
  ++cases;
}

static void DecoderSlots()
{
  State s;
  Harness h(s);
  for (auto p : {"active", "waiting1", "waiting2"})
    h.Decode(p);
  h.Request("active");
  CJob* active = h.jobs.PopJob();
  Check(active != nullptr, "upload occupies one actual pool slot");
  h.Request("waiting1");
  h.Request("waiting2");
  for (unsigned n = 0; n < 4; n++)
    h.New("decode" + std::to_string(n));
  std::vector<CJob*> decoders;
  for (unsigned n = 0; n < 3; n++)
  {
    CJob* decoder = h.jobs.PopJob();
    Check(decoder != nullptr, "normal decoder still admitted with upload in flight");
    decoders.push_back(decoder);
  }
  Check(h.jobs.m_processing.size() == 4 && h.jobs.PopJob() == nullptr,
        "production normal-priority limit applies with no blocked upload waiters");
  Check(h.jobs.added[CJob::PRIORITY_HIGH] == 1, "waiting images did not create pool jobs");
  for (auto* decoder : decoders)
    h.Work(decoder);
  h.Work(active);
  h.Active();
  h.Active();
  ++cases;
}

static void PrefetchBudgetAndReplacement()
{
  State s;
  Harness h(s);
  h.Decode("active");
  h.Request("active");
  for (unsigned n = 0; n < 12; n++)
  {
    const auto name = "old" + std::to_string(n);
    h.Decode(name);
    h.Request(name);
  }
  h.Decode("near");
  frameTime = 16;
  h.Request("near", false, true);
  Check(h.images.m_allocatedLookup.at("near")->m_uploadQueued,
        "visible-cache backlog does not exhaust speculative budget");
  h.Active();
  Check(h.images.m_allocatedLookup.at("near")->m_uploadJobID == h.images.m_activeUploadJobID,
        "fresh nearby row precedes old cache backlog");
  while (h.images.m_activeUploadJobID)
    h.Active();
  for (unsigned n = 0; n < 9; n++)
    h.Decode("pref" + std::to_string(n));
  for (unsigned n = 0; n < 8; n++)
    h.Request("pref" + std::to_string(n), false, true);
  frameTime = 32;
  h.Request("pref8", false, true);
  Check(h.images.m_uploadQueue.size() == 8 &&
            h.images.m_allocatedLookup.at("pref8")->m_uploadQueued,
        "new prediction replaces an older prediction at the limit");
  h.images.ProcessDeferred();
  Check(h.images.m_allocatedLookup.at("pref8")->m_uploadJobID == h.images.m_activeUploadJobID,
        "fresh prediction is scheduled before older predictions");
  while (h.images.m_activeUploadJobID)
    h.Active();
  ++cases;
}

static void WrappedFrame()
{
  State state;
  Harness h(state);
  for (auto path : {"active", "old", "near"})
    h.Decode(path);
  frameTime = ~0u - 15;
  h.Request("active");
  h.Request("old");
  frameTime = 0;
  h.Request("near", false, true);
  Check(h.images.m_uploadFrame == 0, "an explicit zero frame snapshot survives clock wrap");
  h.Active();
  Check(h.images.m_allocatedLookup.at("near")->m_uploadJobID == h.images.m_activeUploadJobID,
        "wrapped fresh demand precedes retained work");
  h.Active();
  h.Active();
  ++cases;
}

int main()
{
  try
  {
    WrappedFrame();
    PrefetchBudgetAndReplacement();
    PriorityAndSerialization();
    DecoderSlots();
    Speculation();
    ReleaseAndReacquire();
    RemovedActiveEntry();
    ReplacedPath();
    RunningCancellation();
    Fallbacks();
    DemandDuringDecode();
    DecodePlacement();
    BackgroundHold();
    FreshVisibleFirst();
    std::printf(
        "PASS %u scenarios, %u checks: actual manager, completion and cancellation methods\n",
        cases.load(), checks.load());
  }
  catch (const std::exception& error)
  {
    std::fprintf(stderr, "FAIL %s\n", error.what());
    return 1;
  }
}
