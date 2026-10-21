/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include "JSONRPC.h"
namespace JSONRPC
{
class CDetectorOperations
{
public:
  static JSONRPC_STATUS GetCapabilities(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS Start(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS GetStatus(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS Cancel(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS Bind(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS GetBinding(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS GetPlayback(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS Apply(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS ReleaseBinding(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS SetSkipNext(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
  static JSONRPC_STATUS ClearSkipNext(
      const std::string&, ITransportLayer*, IClient*, const CVariant&, CVariant&);
};
} // namespace JSONRPC
