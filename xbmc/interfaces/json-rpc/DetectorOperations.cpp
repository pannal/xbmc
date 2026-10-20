/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "DetectorOperations.h"

#include "ITransportLayer.h"
#include "utils/DetectorAdmission.h"
#include "utils/FileUtils.h"
#include "utils/ScopedLogSilence.h"
#include "utils/Variant.h"
#if defined(HAS_LIBAMCODEC)
#include "utils/ActiveAreaDetector.h"
#endif
using namespace JSONRPC;
namespace
{
constexpr bool Supported()
{
#if defined(HAS_LIBAMCODEC)
  return true;
#else
  return false;
#endif
}
void Reply(CVariant& result, const char* status)
{
  result["version"] = 1;
  result["status"] = status;
}
} // namespace
JSONRPC_STATUS CDetectorOperations::GetCapabilities(
    const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant& result)
{
  result["version"] = 1;
  result["supported"] = Supported();
  result["skipproperty"] = DETECTOR_ADMISSION::PROPERTY;
  result["ownedskip"] = Supported();
  result["maxjobs"] = 16;
  result["maxscanners"] = 1;
  result["cache"] = "shared_validated";
  return OK;
}
JSONRPC_STATUS CDetectorOperations::Start(const std::string&,
                                          ITransportLayer* transport,
                                          IClient*,
                                          const CVariant& params,
                                          CVariant& result)
{
  const CScopedLogSilence sensitiveInput;
  if (!Supported())
    Reply(result, "unsupported");
#if defined(HAS_LIBAMCODEC)
  else if (!transport ||
           (!transport->IsLocal() && !CFileUtils::RemoteAccessAllowed(params["path"].asString())))
    Reply(result, "path_not_shared");
  else
    result = aml_detector_start_job(params["path"].asString(),
                                    static_cast<int>(params["options"]["streamindex"].asInteger()),
                                    params["options"]["allowhttp"].asBoolean());
#endif
  return OK;
}
JSONRPC_STATUS CDetectorOperations::GetStatus(
    const std::string&, ITransportLayer*, IClient*, const CVariant& params, CVariant& result)
{
  if (!Supported())
    Reply(result, "unsupported");
#if defined(HAS_LIBAMCODEC)
  else
    result = aml_detector_job_status(params["requestid"].asString());
#endif
  return OK;
}
JSONRPC_STATUS CDetectorOperations::Cancel(
    const std::string&, ITransportLayer*, IClient*, const CVariant& params, CVariant& result)
{
  if (!Supported())
    Reply(result, "unsupported");
#if defined(HAS_LIBAMCODEC)
  else
    result = aml_detector_cancel_job(params["requestid"].asString());
#endif
  return OK;
}
JSONRPC_STATUS CDetectorOperations::SetSkipNext(
    const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant& result)
{
  Reply(result, Supported() ? "armed" : "unsupported");
  if (Supported())
    result["token"] = std::to_string(DETECTOR_ADMISSION::Publish("1"));
  return OK;
}
JSONRPC_STATUS CDetectorOperations::ClearSkipNext(
    const std::string&, ITransportLayer*, IClient*, const CVariant& params, CVariant& result)
{
  Reply(result, Supported() ? "unchanged" : "unsupported");
  if (!Supported())
    return OK;
  try
  {
    const auto text = params["token"].asString();
    const auto token = std::stoull(text);
    if (std::to_string(token) != text)
      return InvalidParams;
    if (DETECTOR_ADMISSION::Cancel(token))
      result["status"] = "cleared";
  }
  catch (...)
  {
    return InvalidParams;
  }
  return OK;
}
