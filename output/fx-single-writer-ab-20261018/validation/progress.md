# SDD ledger — plan: docs/superpowers/plans/2026-10-18-single-writer-market-migration.md

User approved implementation with sol agents, including execution-time risk, precise history exception, existing User lock instead of account queue.
Baseline: 330f4e6. Isolated branch perf/single-writer-markets-20261018. Root main dirty changes and draft 6526700 untouched.

## Preflight / task map
| Tasks | Shared contract | Finding |
|---|---|---|
| 1 FX core / 2 FX other writers | primitive commands, owned session, commit/install/notify | Sequential; 2 builds on 1 |
| 1+2 FX / 3 LMSR | independent economic writer modules; account_events shared only by 3 | Can implement in parallel; main.py and conftest owned by FX |
| 1+2+3 / 4 reads+risk | existing target GATE temporarily kept; final caller checks removed together | Stage 4 only after writer coverage review |
| 4 / 5 validation+perf | final immutable snapshot and User lock semantics | PG and load sequential, final commit fixed before A/B |
| 1 | core route and tests agree | use DB core, no copied AMM |
| 2 | lifecycle, order and PvE boundaries explicit | no nested actor waits; preserve two PvE transactions |
| 3 | lifecycle and notification order explicit | internal recovery direct, no self submit |
| 4 | cold/read/risk contract explicit | draft code reference only; risk must use published snapshots |
| 5 | verification proportionate | preserve money/history assertions; no blanket TDD/new CI |

Task 1: pending — FX core writer and lifecycle, spot/borrow/net/short.
Task 2: pending — FX orders/liquidation/liquidity/admin/PvE/writeoff coverage.
Task 3: pending — LMSR lifecycle and publish-before-notify.
Task 4: pending — ordinary reads/risk and remove GATE.
Task 5: pending — final review, PG concurrency, A/B, integration preserving root changes.

Project AGENTS overrides skill blanket TDD/full-suite requirements; approved design permits reversible worktree setup. No push/deploy.

## Dispatch
- Plan commit 070ff4d. Root-before manifest 515 source files; 9 root-vs330 unrelated files recorded separately, preserve during integration.
- Task 1 running: /root/fx_writer_core_impl (gpt-6.1-sol high). Minimal pve_fx/executor.py producer update authorized to eliminate callback command; observation migration remains Task2.
- Task 3 running: /root/lmsr_writer_lifecycle_impl (gpt-6.1-sol high). Narrow optional deferred notifying_transaction helper backward-compatible, LMSR outcome returns descriptors for publication after apply/install.
- Task 4 read-only preflight running: /root/snapshot_risk_preflight. No production ownership until writer stages reviewed.
- No services/PG/load running; no root source modified.

Task4 preflight complete: risk currently SQL, existing read records sufficient, global fee stays rule; own locked account discovery before simulation needed to avoid doubleinterest and stale absolute shortdebt. Report task-4-preflight.md.
Task5 harness preparation running /root/single_writer_ab_prepare; no services/load authorized before finalcode signal.

Task3 implementation 473498e; reported 34 + 24 scoped tests passed, diff/AST clean. Task review running /root/lmsr_lifecycle_review with fixed 070ff4d..473498e package. Not marked complete until review.

Task 3: complete (commits 070ff4d..473498e, review clean). Readonly reviewer /root/lmsr_lifecycle_review, task-3-review.md. No actionable/deferred findings.
Task5 A/B prep complete /root/single_writer_ab_prepare; baseline369files hash matches330, same workload/protection, runtime/output fx-single-writer-ab-20261018. Optional GATE observer and queryelapsed/PG observed locks distinguished. After and FX writer observer pending final committed code. No DB/services/load started.

Task1 implementation98e846e, fixedreview473498e..98e846e. Report189passed+36affectedpassed, oneSQLite connectioncleanupwarning baselineunknown at foreign_only_short_blocks_lmsr_writer_buy, focusedreview assessing. PG fixture adapted but PGnotrun. Review/root/fx_core_writer_review running.
Guide plannedupdate /root/single_writer_guide_update owns guide.tex/pdf only; copied root guide to isolatedbranch, no rootdocsmodified.
Temporary PG stopped: no postmaster.pid; saved postmaster.opts includes socket55489 noTCP, pg_stat_statements+I/Otracking. Controller will start only scheduledvalidation/load.

Task 1: complete (commits473498e..98e846e, review approved). No Critical/Important. Minor deferred: FX writer stop during unexpected-failure recovery can leave active future until10sec submit timeout (writer.py205), no financial corruption; finalwholebranchreview must triage. Warning-focused same node current1passed0.22s vs baseline1passed0.24s neitherwarning, suitewarning unlocalized, no proven actorleak.
Task2 running /root/fx_writer_remaining_impl sol6.1high; baseline98e846e. Scoped brief already records allremainingowners, actualactorcontract in task1report.
A/B agent resumed preparationonly to add stable98eactor observer, noafterextract/services/load.

TaskGuide4ce4081 complete implementation (tex+PDF12pages, 2xelatexexit0, zero layoutwarnings perreport); actualmodulefactcheck remains finalwholebranchreview. Task2 reviewbase advanced to4ce4081 because docscommittedbeforeTask2code, noFXcommits excluded.
Ruling: split Task4a ordinaryreads/synccommittedaccessor as independentparallelwork whileTask2 completes — alreadyapproved readsemantics and old330writercommitowners alreadyinstall; no riskactivation/GATEdelete untilwritercoverage — cost ifwrong localreadrework, no unreviewedfinancialroute removal.

Task4a /root/ordinary_snapshot_reads_impl running independentreadslice. Acceptedproducedcontract PricingRegistry.capture_committed_records(Sequence[GroupKey])->dict[tuple[str,int],PricingRecord], sync noIO, onlyhealthyownerreusableentries; missingomitted and riskmustreject. Marketfinancial/GATEcleanup untouched.
Task2 initial17triggerchecks pass; existingfixturebootstrap extended toregister_pair onlytestside, productionstartup explicit.
A/B observer preparation complete: stablecore98e actorqueuewrap puts/gets, queuedaccepted->dequeued wait; execution dequeued->nextget/exittime includeshandler/commit/install/hints/future/recovery, excludesidle. Beforeactorabsent reportedN/A; SQL attributionactor/background separate. Noafterextractorload, readinessfalse.

Task4a implementation809ad3f, fixedreview4ce4081..809ad3f /root/ordinary_snapshot_reads_review running. Tenownedfiles, reads27+quotes22+frontend19passed, PGreadtestsadaptedbutnotrun. Task2 reviewBASE advanced809ad3f asallFXchangesstilluncommitted.
RuntimeRegistry startupconfigall{fx,lmsr}; rootunitsetup resetsmemory_products empty. Task4b mustexplicitwarm/configure fixturepublishedrecords, noSQLfallbackbypass.

Task 4a: complete (commits4ce4081..809ad3f, review clean). PGreadtests pendingfullvalidation, notPGproof.
Task2 implementationc53ccd3, review809ad3f..c53ccd3 /root/fx_remaining_writer_review running; reported145pass+corrected5pass+fixtureconsumers30pass, nofinalwarnings, PGpending.
Namedintegrationcheck: root realtime.py vs330 differs onlyIpConcurrencyLimiter.MAX_PER_IP10(root)vs200(benchmarkbaseline). Preserve root10, no migrationedit. A/B bothfixedarchives retain200 tosupportsame-loopback100users; don'tclaimrootIPpolicychanged. Other9rootdiffs preserved.

Task 2: complete (commits809ad3f..c53ccd3, review clean). AllplannedFXremainingwriterowners auditedandapproved; realPGvalidationpending. Queue financial anddelete/liquiditynotificationsverifiedbyreview.
Task4b nowauthorizedactivationafterTask1/2/3/4areviewcomplete. CurrentHEADc53ccd3, nootherproductioneditor.

Task4b production executableGATErefs removed, oldmodule/deadmodule-onlytests deleted; activeunitmigrationstillinprogress. Fourdelegatedtestfiles6b69f4e parent5978b3b, reported61retainedpassed+3strengthenedrerun, task-4-tests-report.md. OverallTask4reviewBASE5978b3b willincludepeerunitcommitandfutureproductioncommit.
Testfixture published_markets opt-in wraps onlymodule-local committedseed helpers and calls explicitpublish; inspected44lines, no executioncommit hook orproductionfallback. Import helper fromtests.conftest.
PGstartedbycontroller withsameisolation/noTCP/socket55489/pgstat+IO; newexclusiveDB fx_single_writer_correctness_test created. Task4b ownsPGtests exclusively; URLsent, no benchmarkservices/load. MustreleasePGslot/alltestprocessesexitedbeforeload.

Task4b PGfirst45cases:37pass4fail4fixtureerrors; localized8/9passed afterfixes. NewtwoFXwriterfinancechecks secondfreshcash30/debt70/firstholding/version2 (existingloan+tradebumptwice) anddenialcreditnotcash. PGshort15passed. FixturehardcodedDBallowlist/coldRegistryconfig/historycommitspiesfixed, nofundassertionrelaxation.
Foundactualgap: liquidationvalue_user_detailed SQLq/reserves mixedwithpreparedpublishedinputs for ranking/executionthreshold; workerfixesoptionalpreparedvaluationpath preservingformulas. Postactionactualtargetpost+freshownledger+sameforeigncaptures. 100mixedscan95/100actions under3boundedselectionretries, investigateaftercoherentinputs.
Ruling: preserveexistingliquidationranking and3retrypolicy; ifgenuineunfrozenpricedriftpersists, adapt obsoletefirstpass100guarantee usingoneboundedexistingnextpassafterwavequiescence (sequentialretryablesifneeded), preservefinal100fund/action/version/idempotencyasserts andreportfirstpassretrysemantics — acceptsuser-approvednofreezewithalreadyexistingsweepretry, avoidsnewalgorithm — costifwrong staleeligibilitycouldhideunlessfirstwaveoutcomesandfinalinvariantsasserted. Noartificialpricefreeze/unboundedretryuntilgreen.
PGexclusiveTask4b stillheld, processespreviousrunexitedbutfix/reruncontinuing; nobenchmarkload.

Task4b preparedvaluation consistencyfixcomplete, focusedPG3liquidationpassed; postactualtarget/freshownledger/sameforeigncaptureunitpassed. 100mixedscanfirstwave94/95 +retry_exhausted, oneboundedexistingnextpassperretryableafterwavequiesced yieldsfinal100uniqueactions/events/users; originalfinancialcountsassertsretained, productionranking/3retryunchanged.
FinalPG45-caseevidence44passed+oneassertion-onlyversionfixturemistakecorrected/localcasepassed0.76s; sourceunchangedfor44cases, noPGskipsreported. Task4bPGprocessesexited andPGslotexplicitlyRELEASED; serverstillrunningonlycontroller. No benchmarkloadbeforefinalcodeandunitprocessesexited. Final354relatedunitssliceactive,someoldcoldfixtureassertionspendinglocalfix.

Task 4b: complete (commits5978b3b..b413408, review approved noCritical/Important). Optionalminor stalecomments risk.py622 snapshotsfill/net_trading.py262 oldgatecontract; finalreviewtriage. Correctness45PG/354relatedunitseachpassingviareportedfull+targetedreruns; allprocessesexit. Broadfinalreviewnext, thenfixedfinalrefA/B.

Finalwholebranchreview /root/single_writer_final_review APPROVED330f4e6..b413408, noCritical/Important. ThreeMinor groups: boundedshutdownresponse; stalerisk/net/admin/liqcomments; guideindexprefix. Onefinalfixwave /root/single_writer_final_doc_cleanup ownscomments+guidePDFonly, nofunctionalchanges/newtests, compile2XeLaTeX/diffcheck; afterwardonescopedrereview.
Ruling: park boundedshutdown-recoveryactivefuture10sectimeout — occursduringstop/recovery, nofund/statecorruption oractive-tradingimpact, alignsuserlowprobobservableboundarypolicy — costifwrong delayedshutdownresponseuntiloriginaltimeout; recordasfollowup, no newrecoverymechanism.

## Remaining execution map (do not redispatch completed stages)
1. Awaitfinaldoccleanupcommit/report; generateb413408..FIXHEADpackage, followupfinalrevieweronescopedrereview. Functionalcodeotherwiseapproved.
2. NotifybenchmarkagentFINALREFonlyaftercleanrereview; thenprepareafterarchive/readinessmanifest; separateexplicitSTART signal. Executeexactharnessrun --start-authorized once, sameworkloadbefore330f4e6/afterfixedref, PGstillrunningcontrollerowned. NootherCPUtests/loadparallel. AgentonlyownsHTTP/monitorcleanup/tokenremoval, notPGstop.
3. ReadactualrawA/B+analysisreport; nofuturethroughputpromises; preserveaggregate25cycles/scontext/100users/200SSEandprotectionstops, recordfirst-passliqretry/coldhistoryexceptions.
4. RootintegrationAUTHORIZEDbyuserimplementation, noextraapproval: rootmainc0bebd8dirtyoriginalfilespreserve. Beforechecksuse root-before.json515sourcehash + root-docs-before.json; generatedbranch330..finalsourcepatchforbackend/app,backend/tests,frontendstore,docs/api excludingnewguide/planexistinguntrackedroot. gitapply --check thenapply, no--index/reset/stash/broadstage. Rootapi/market.py unrelatedleaderboardhunks mustsurvive, rootrealtimeIPcap10unchanged (branchbench200). Newplan+guide.tex/pdf rootalreadyexist: onlycopyifcurrenthashmatchesmanifest, preservingunrelatedMakefile. Recordtargetbackupsbeforeapply. Remainingsource9unrelateddifferentmanifestentries listedroot-vs-baseline.json.
5. Runproportionaterootboundarychecksafterintegration (samecodeaside9unrelatedsources), nofullrepo/UIinteraction. Updateplancheckboxes/durablevalidation/code-review/guidereviewafteractualsuccess; docs-onlydeliverycommitmayfollowmeasuredbackendref ifbackendhashesunchanged.
6. Copyallignoredstage/finalreviews/fixes+ledgerdecisions todurableoutput/fx-single-writer-ab-20261018 beforeanyskillworkspacecleanup. StoponlyourHTTPmonitorsandthenPGclusterwhenalltests/loadended; no push/deploy. FinalChineseresponse importantchanges,actualperf/PG/rootchecks,materialhistory/liqfollowupboundaries andlinks.

Finaldoccleanup6c76757 scopedrereview APPROVED; noexecutablechange/newissue, boundedshutdownminorstilldeferred. FixedFINALREF6c7675722cda265bd205dced6ce0f939ae7ac769 sentbenchmarkagent preparationonly, separateSTARTpending. Root521source/docshashesunchanged; sourcepatch330..6c76757 gitapply--check0, notappliedyet.

FixedA/Bcompleted bothstressP99>2sprotection/burstunrun. Root70paths integrated,454unrelatedhashespreserved,rootIp10preserved. Root7modules115passed22.18s,gitdiff--check0. ControllerPGfaststop0 afterallHTTP/monitor/driverexit. Finalreport/doccommitpending.

DELIVERY COMPLETE: finalChineseABreportreadbycontroller,rawkeyvaluesindependentlychecked. Functionalmeasurement6c76757; docs-onlycompletion58101e1/backendunchanged. Rootintegrated/115passed/diff0,allrelevantreviewsdurable,PGandHTTPstopped,tokensremoved. Branchandisolatedworktreepreserved,no push/deploy. Followup: CPU/writerexecutionoptimizationseparateproposal; boundedshutdownminorparked; exacthistoryDBwaitretained.
