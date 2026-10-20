/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include <string>
class CVariant;
// Explicit asynchronous jobs do not modify the current player's detector source.
CVariant aml_detector_start_job(const std::string& path, int streamIndex, bool allowHTTP);
CVariant aml_detector_job_status(const std::string& id);
CVariant aml_detector_cancel_job(const std::string& id);
bool aml_detector_retire_jobs();
