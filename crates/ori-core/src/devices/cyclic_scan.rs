//! Cyclic AR-DV1 progress comes from committed RX originals. The actor owns
//! only its current physical segment and receives one global point at a time.
use super::*;
use ori_instruments::CyclicReceiverScope;

const MAX_PASS: u64 = 9_007_199_254_740_991;

#[derive(Clone, Copy)]
pub(super) struct Cursor {
    pass: u64,
    pub index: usize,
}

struct Progress {
    next: Cursor,
    last: Option<(Cursor, String)>,
    held: bool,
    manual_tuned: bool,
}

pub(super) struct Scan {
    plan: CyclicReceiverScope,
    key: String,
    progress: Mutex<Progress>,
}

impl Scan {
    pub async fn restore(
        archive: &Archive,
        stream: &DeviceStream,
        plan: CyclicReceiverScope,
    ) -> DeviceResult<Self> {
        // Bind a checkpoint to the original receiver port and stream settings.
        // Display names and lifecycle state can change between connections.
        let key = ori_guard::ardv1_plan_key(stream)
            .map_err(|error| DeviceError::Invalid(error.to_string()))?;
        let scan = Self {
            plan,
            key,
            progress: Mutex::new(Progress {
                next: Cursor { pass: 1, index: 0 },
                last: None,
                held: false,
                manual_tuned: false,
            }),
        };
        if let Some(original) = archive
            .latest_receiver_scan_point(&stream.session_id, &stream.id, &scan.key)
            .await?
        {
            let invalid =
                || DeviceError::Worker("Invalid archived cyclic receiver checkpoint".into());
            let meta = &original["metadata"];
            let point = Cursor {
                pass: meta["scan_pass"]
                    .as_u64()
                    .filter(|n| (1..=MAX_PASS).contains(n))
                    .ok_or_else(invalid)?,
                index: meta["scan_index"]
                    .as_u64()
                    .and_then(|n| usize::try_from(n).ok())
                    .ok_or_else(invalid)?,
            };
            if original["session_id"] != stream.session_id
                || original["stream_id"] != stream.id
                || meta["scan_plan_key"] != scan.key
                || meta["scan_total"] != plan.points()
                || meta["scan_start_hz"] != plan.start_hz()
                || meta["scan_stop_hz"] != plan.stop_hz()
                || meta["scan_step_hz"] != plan.step_hz()
                || meta["settle_s"].as_f64() != Some(plan.settle().as_secs_f64())
                || meta["scan_strategy_requested"] != plan.strategy()
                || meta["scan_segment_index"].as_u64()
                    != plan.segment_index(point.index).map(|n| n as u64)
                || !scan.verified_point(point, &original)
                || original["id"].as_str().is_none()
            {
                return Err(invalid());
            }
            scan.committed(point, &original);
        }
        Ok(scan)
    }

    fn verified_point(&self, point: Cursor, original: &Value) -> bool {
        let meta = &original["metadata"];
        let Some(frequency) = self.plan.frequency_at(point.index) else {
            return false;
        };
        original["frequency_hz"] == frequency
            && original["mode"] == "FM"
            && original["level_unit"] == "s_meter_count"
            && original["level"].is_number()
            && meta["scope_source"] == "sequential_receiver_readback"
            && meta["simulated"] == false
            && meta["frequency_source"] == "receiver_RX"
            && meta["frequency_readback"] == true
            && meta["requested_frequency_hz"] == frequency
            && meta["observed_frequency_hz"] == frequency
            && meta["requested_mode"] == "FM"
            && meta["observed_mode"] == "FM"
    }

    pub fn stamp(&self, point: Cursor, original: &mut Value) -> DeviceResult<()> {
        if point.pass > MAX_PASS {
            return Err(DeviceError::Worker(
                "AR-DV1 scan pass exceeds archive limits".into(),
            ));
        }
        if !self.verified_point(point, original) {
            return Err(DeviceError::Worker(
                "RX did not verify the planned scan point".into(),
            ));
        }
        let meta = &mut original["metadata"];
        meta["scan_plan_key"] = json!(self.key);
        meta["scan_pass"] = json!(point.pass);
        meta["scan_index"] = json!(point.index);
        meta["scan_total"] = json!(self.plan.points());
        meta["scan_start_hz"] = json!(self.plan.start_hz());
        meta["scan_stop_hz"] = json!(self.plan.stop_hz());
        meta["scan_step_hz"] = json!(self.plan.step_hz());
        meta["scan_segment_index"] = json!(
            self.plan
                .segment_index(point.index)
                .expect("verified scan point")
        );
        meta["scan_strategy_requested"] = json!(self.plan.strategy());
        Ok(())
    }

    pub fn committed(&self, point: Cursor, original: &Value) {
        let mut progress = self
            .progress
            .lock()
            .expect("receiver scan progress poisoned");
        progress.last = Some((
            point,
            original["id"]
                .as_str()
                .expect("committed observation ID")
                .into(),
        ));
        progress.next = if point.index + 1 == self.plan.points() {
            Cursor {
                pass: point.pass + 1,
                index: 0,
            }
        } else {
            Cursor {
                index: point.index + 1,
                ..point
            }
        };
    }

    pub fn next_point(&self) -> Option<Cursor> {
        let progress = self
            .progress
            .lock()
            .expect("receiver scan progress poisoned");
        (!progress.held).then_some(progress.next)
    }
    pub fn held(&self) -> bool {
        self.progress
            .lock()
            .expect("receiver scan progress poisoned")
            .held
    }
    pub fn waiting_for_tune(&self) -> bool {
        let progress = self
            .progress
            .lock()
            .expect("receiver scan progress poisoned");
        progress.held && !progress.manual_tuned
    }
    pub fn manual_tuned(&self) {
        self.progress
            .lock()
            .expect("receiver scan progress poisoned")
            .manual_tuned = true;
    }
    fn set_held(&self, held: bool) {
        let mut progress = self
            .progress
            .lock()
            .expect("receiver scan progress poisoned");
        progress.held = held;
        progress.manual_tuned = false;
    }
    pub fn report(&self, state: &str) -> Value {
        let p = self
            .progress
            .lock()
            .expect("receiver scan progress poisoned");
        let state = if state == "scanning" && p.held {
            "held"
        } else {
            state
        };
        json!({
            "state":state, "next_pass":p.next.pass, "next_index":p.next.index,
            "completed_passes":p.next.pass - 1, "partial":p.next.index != 0,
            "start_hz":self.plan.start_hz(), "stop_hz":self.plan.stop_hz(), "step_hz":self.plan.step_hz(),
            "total_points":self.plan.points(), "segment_index":self.plan.segment_index(p.next.index),
            "last_pass":p.last.as_ref().map(|(c,_)|c.pass), "last_index":p.last.as_ref().map(|(c,_)|c.index),
            "last_observation_id":p.last.as_ref().map(|(_,id)|id)
        })
    }
    pub fn project(&self, status: &mut WorkerStatus) {
        let failed =
            matches!(status.state.as_str(), "error" | "degraded") || status.consecutive_errors > 0;
        let state = if failed {
            "interrupted"
        } else if !status.alive || status.stopping == Some(true) {
            "stopped"
        } else if status.current["restart_required"] == true {
            "interrupted"
        } else if status.state == "running" {
            "scanning"
        } else {
            "starting"
        };
        if failed {
            for key in [
                "frequency_hz",
                "mode",
                "observed_frequency_hz",
                "observed_mode",
            ] {
                status.current[key] = Value::Null;
            }
            status.current["frequency_source"] = json!("unknown");
            status.current["mode_source"] = json!("unknown");
            status.current["frequency_readback"] = json!(false);
        }
        let report = self.report(state);
        status.current["scan_total_points"] = json!(self.plan.points());
        status.current["scan_completed_points"] = report["next_index"].clone();
        status.current["receiver_scan"] = report;
    }
}

impl DeviceManager {
    pub(crate) async fn set_receiver_scan_hold(
        self: &Arc<Self>,
        target: Target,
        hold: bool,
        gate: ExecutionGate,
    ) -> DeviceResult<WorkerStatus> {
        let owner = self.capture_worker(&target, gate)?;
        let worker = owner.original_worker();
        if worker.cyclic_scan.is_none() {
            return Err(DeviceError::Invalid(
                "Receiver stream has no cyclic scan".into(),
            ));
        }
        let (response, result) = oneshot::channel();
        worker
            .commands
            .try_send(Command::ReceiverScanHold {
                hold,
                gate: owner.execution_gate(),
                response,
            })
            .map_err(|error| match error {
                mpsc::error::TrySendError::Closed(_) => DeviceError::Closed,
                mpsc::error::TrySendError::Full(_) => DeviceError::QueueFull,
            })?;
        result.await.map_err(|_| DeviceError::Closed)?
    }
}

impl Worker {
    pub(super) async fn apply_scan_hold(
        self: &Arc<Self>,
        hold: bool,
        gate: ExecutionGate,
    ) -> DeviceResult<WorkerStatus> {
        self.live()?;
        gate.check(Some(self))?;
        let scan = self.cyclic_scan.as_ref().ok_or(DeviceError::Denied)?;
        if scan.held() != hold {
            if hold {
                let Some(Native::Receiver(actor)) = &self.native else {
                    return Err(DeviceError::Denied);
                };
                self.active_io.store(true, Ordering::Release);
                let result = async {
                    actor
                        .enqueue_checked(
                            ArDv1Request::HoldCyclicScope,
                            self.actor_gate(gate.clone(), false, None),
                        )?
                        .result()
                        .await
                }
                .await;
                self.active_io.store(false, Ordering::Release);
                if !matches!(result?, ArDv1Reply::Ready) {
                    return Err(DeviceError::Worker("Unexpected AR-DV1 hold reply".into()));
                }
            }
            // The worker serializes this transition with polling and commits.
            // Invalidate any held manual tune evidence before scanning can restart.
            self.applied_revision.fetch_add(1, Ordering::AcqRel);
            scan.set_held(hold);
            if let Err(error) = self.live().and_then(|()| gate.check(Some(self))) {
                // A lost return permit cannot start another scan point.
                if !hold {
                    scan.set_held(true);
                }
                return Err(error);
            }
        }
        Ok(self.snapshot())
    }
}
