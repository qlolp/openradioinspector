# macOS post-test reliability and complete RC1 acceptance
Approved spec: user's conversation plan,16 November2026. BASE f4330d1e057cbd8c425e53b7097d61c753620afb.

## Global constraints
Preserve API/archive/IDs/integerHz/UTC/values/units,20MiB/s backup accounting,strict JSON/framing,cursor/ACK ordering,independent instrument owners,RF off. No auto deletion/merge/stablepublication/audio/scientificworker. One unchanged finalarm64app+kit must close gates. Smoke is never point2/point4. Physical USB/sleep actions require actual observations.

## Task 1: Backup budgets and independent evidence (PR1)
RED first. Derive copy=max(300,ceil(3*S/R)+60),total=max(600,ceil(9*S/R)+120),R=20971520,from archive-owner capacity S,reject0/>16GiB. Emit admitted private budget/phase. Controller and restore consume matching deadlines with bounded start/cleanup. Evaluate closed native evidence even after backup/restore errors; retain component and cleanup outcomes. All gates required for overallpass.
Expected: acceptancePython,real packagedCLIbackup/restore,Rustacceptance tests and available fullreleasegate pass before publication.

## Task 2: Native delivery/render path (PR2)
Measure transport/decode/delivery/preparation/application/presentation and queues with exactrun/build/source identity and actualnegotiatedtransport. Profile fullMacwindow. Keep ACK after modelapplication. Immutable offMainActor renderingdata,isolated panelstate,incremental waterfall/envelope/geometry caches,no wholehistory work in drawing. Preservemetadata-race/contextownership/tabs/selections/features.
Expected: RED→GREENregressions,fullSwift/native/SDK+Xcodegates,realshortnative smoke; fullmeasurements stillrequired.

## Task 3: Complete kit and protocol (PR3)
PreserveoldCLI; addpreflight,native60ssmoke,auto/sse/binary-v1testtransport. Private localApplicationSupport default. Preflight actualOS/RAM/power/geometry/TCC/disk/fullpeakbudget/owners/identity. Certify UIclosed>=30s,USBabsent>=10s,realsleep>=10s. Preservepartialresults. Bounded install/service/update/rollback/offline/security/fault/history tests+operatorprotocol. ReproduceARMpressure/MacTk/APIparityfixture timing without weaker deadlines.
Expected: allrelevant suites+fullCIgreen,relocatedkit selfchecktwice,no unexplainedskips.

## Task 4: Final review and immutable Mac acceptance
One freshwholebranch reviewer; fixCritical/Important RED→GREEN,no per-taskagents. Freezeapp/core/supervisor/source. Fullrunners+CI,preflight,install/lifecycle/fault/million*290history,60ssmoke,two30minloads(auto/SSE),each18000*4096sweeps+18000observations+history+online/finalbackup/fullrestore,thenlive8htinySA+ARDV1.
Gates: allofferedsourcesexact/presented; p95UI<=100ms,queue<=200ms,commit→shown<=250ms;pan>=30fps;first100warmmillionhistory<=500ms;core<=256MiB/UI<=1GiB/background<=1.5GiB. No filtereddenominator,timestamp substitution,hiddenretry orsimulatedphysicalacceptance. Weeklongrealendurance+broaderusability laterthanpersonalRC1.
