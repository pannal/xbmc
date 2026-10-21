/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include <string>
class CVariant;
// Explicit asynchronous jobs do not modify the current player's detector source.
CVariant aml_detector_start_job(const std::string& path, int streamIndex, bool allowHTTP);
CVariant aml_detector_job_status(const std::string& id);
CVariant aml_detector_cancel_job(const std::string& id);
CVariant aml_detector_bind_job(const std::string& id, const std::string& path,
                              int streamIndex, bool sameVideo);
CVariant aml_detector_binding_status(const std::string& id);
CVariant aml_detector_playback_status();
CVariant aml_detector_apply_binding(const std::string& id, const std::string& playback,
                                   const std::string& selection);
CVariant aml_detector_release_binding(const std::string& id);
bool aml_detector_retire_jobs();
