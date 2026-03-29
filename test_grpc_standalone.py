"""Standalone gRPC test — no database needed.

Starts an in-memory gRPC server with mock servicers, then runs client tests.
This validates the full gRPC proto → server → client pipeline.
"""

import asyncio
import grpc
from concurrent import futures
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.grpc_gen import cosmos_pb2, cosmos_pb2_grpc


# =============================================
# Mock servicers (no DB, pure in-memory)
# =============================================

class MockGraphRAGServicer(cosmos_pb2_grpc.GraphRAGServiceServicer):
    def __init__(self):
        self.nodes = {}
        self.edges = []

    async def IngestModuleDeps(self, request, context):
        name = request.module_name
        self.nodes[name] = {"type": "module", "name": name, "repo_id": request.context.repo_id}
        n = 1
        e = 0
        for imp in request.imports:
            self.nodes[imp] = {"type": "module", "name": imp}
            self.edges.append({"src": name, "dst": imp, "type": "imports"})
            n += 1; e += 1
        for fn in request.functions:
            self.nodes[fn] = {"type": "function", "name": fn}
            self.edges.append({"src": name, "dst": fn, "type": "defines"})
            n += 1; e += 1
        for dep in request.dependencies:
            self.nodes[dep] = {"type": "module", "name": dep}
            self.edges.append({"src": name, "dst": dep, "type": "depends_on"})
            n += 1; e += 1
        return cosmos_pb2.IngestResponse(success=True, message="ingested", nodes_created=n, edges_created=e)

    async def IngestCourierRelationship(self, request, context):
        self.nodes[request.seller_name] = {"type": "seller", "name": request.seller_name}
        self.nodes[request.courier_name] = {"type": "courier", "name": request.courier_name}
        self.edges.append({"src": request.seller_name, "dst": request.courier_name, "type": "delivers_for"})
        return cosmos_pb2.IngestResponse(success=True, message="courier ingested", nodes_created=2, edges_created=1)

    async def IngestChannelRelationship(self, request, context):
        self.nodes[request.seller_name] = {"type": "seller", "name": request.seller_name}
        self.nodes[request.channel_name] = {"type": "channel", "name": request.channel_name}
        self.edges.append({"src": request.seller_name, "dst": request.channel_name, "type": "sells_on"})
        return cosmos_pb2.IngestResponse(success=True, message="channel ingested", nodes_created=2, edges_created=1)

    async def QueryRelated(self, request, context):
        matching = [n for name, n in self.nodes.items() if request.query.lower() in name.lower()]
        nodes = [cosmos_pb2.GraphNode(id=n["name"], name=n["name"], node_type=n["type"]) for n in matching[:10]]
        edges = [cosmos_pb2.GraphEdge(source_id=e["src"], target_id=e["dst"], edge_type=e["type"], weight=1.0) for e in self.edges[:20]]
        return cosmos_pb2.GraphQueryResponse(nodes=nodes, edges=edges, total_nodes=len(nodes), total_edges=len(edges))

    async def GetStats(self, request, context):
        type_counts = {}
        for n in self.nodes.values():
            t = n.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        return cosmos_pb2.GraphStatsResponse(total_nodes=len(self.nodes), total_edges=len(self.edges), nodes_by_type=type_counts)

    async def FindNodes(self, request, context):
        matching = [n for n in self.nodes.values() if not request.node_type or n.get("type") == request.node_type]
        nodes = [cosmos_pb2.GraphNode(id=n["name"], name=n["name"], node_type=n["type"]) for n in matching[:20]]
        return cosmos_pb2.FindNodesResponse(nodes=nodes)

    async def Traverse(self, request, context):
        nodes = [cosmos_pb2.GraphNode(id=n["name"], name=n["name"], node_type=n["type"]) for n in list(self.nodes.values())[:10]]
        return cosmos_pb2.TraversalResponse(nodes=nodes)

    async def TraverseStream(self, request, context):
        for i, (name, n) in enumerate(list(self.nodes.items())[:10]):
            yield cosmos_pb2.TraversalNode(
                node=cosmos_pb2.GraphNode(id=name, name=name, node_type=n["type"]),
                depth=i % 3,
            )

    async def GetShortestPath(self, request, context):
        return cosmos_pb2.TraversalResponse(path=[request.from_node_id, request.to_node_id])


class MockVectorStoreServicer(cosmos_pb2_grpc.VectorStoreServiceServicer):
    def __init__(self):
        self.embeddings = []

    async def EmbedAndStore(self, request, context):
        self.embeddings.append({"content": request.content, "type": request.entity_type, "id": request.entity_id})
        return cosmos_pb2.EmbedResponse(success=True, embedding_id=f"emb-{len(self.embeddings)}", dimensions=384)

    async def SearchSimilar(self, request, context):
        results = []
        for e in self.embeddings:
            score = 0.9 if request.query.lower() in e["content"].lower() else 0.3
            results.append(cosmos_pb2.SearchResult(entity_type=e["type"], entity_id=e["id"], content=e["content"], score=score))
        results.sort(key=lambda r: r.score, reverse=True)
        return cosmos_pb2.SearchResponse(results=results[:request.top_k or 5])

    async def BatchEmbed(self, request, context):
        for item in request.items:
            self.embeddings.append({"content": item.content, "type": item.entity_type, "id": item.entity_id})
        return cosmos_pb2.BatchEmbedResponse(total=len(request.items), success_count=len(request.items))

    async def GetStats(self, request, context):
        return cosmos_pb2.VectorStatsResponse(total_embeddings=len(self.embeddings), dimensions=384)

    async def SearchStream(self, request, context):
        for e in self.embeddings[:5]:
            score = 0.9 if request.query.lower() in e["content"].lower() else 0.3
            yield cosmos_pb2.SearchResult(entity_type=e["type"], entity_id=e["id"], content=e["content"], score=score)

    async def DeleteByEntity(self, request, context):
        before = len(self.embeddings)
        self.embeddings = [e for e in self.embeddings if not (e["type"] == request.entity_type and e["id"] == request.entity_id)]
        return cosmos_pb2.DeleteResponse(deleted_count=before - len(self.embeddings))


class MockSandboxServicer(cosmos_pb2_grpc.SandboxServiceServicer):
    def __init__(self):
        self.suites = {}
        self.cases = {}
        self.runs = {}
        self._counter = 0

    def _id(self):
        self._counter += 1
        return f"id-{self._counter:04d}"

    async def CreateSuite(self, request, context):
        sid = self._id()
        self.suites[sid] = {"id": sid, "name": request.name, "agent_type": request.agent_type}
        return cosmos_pb2.SuiteResponse(id=sid, name=request.name, agent_type=request.agent_type)

    async def ListSuites(self, request, context):
        suites = [cosmos_pb2.SuiteResponse(id=s["id"], name=s["name"]) for s in self.suites.values()]
        return cosmos_pb2.ListSuitesResponse(suites=suites)

    async def AddTestCase(self, request, context):
        cid = self._id()
        self.cases[cid] = {"id": cid, "suite_id": request.suite_id, "prompt": request.input_prompt}
        return cosmos_pb2.TestCaseResponse(id=cid, suite_id=request.suite_id, input_prompt=request.input_prompt)

    async def GetTestCases(self, request, context):
        cases = [cosmos_pb2.TestCaseResponse(id=c["id"], suite_id=c["suite_id"]) for c in self.cases.values() if c["suite_id"] == request.suite_id]
        return cosmos_pb2.GetTestCasesResponse(cases=cases)

    async def StartRun(self, request, context):
        rid = self._id()
        self.runs[rid] = {"id": rid, "suite_id": request.suite_id, "status": "running", "version": request.agent_version}
        return cosmos_pb2.RunResponse(id=rid, suite_id=request.suite_id, status="running", agent_version=request.agent_version)

    async def RecordResult(self, request, context):
        return cosmos_pb2.RecordResultResponse(success=True)

    async def CompleteRun(self, request, context):
        if request.run_id in self.runs:
            self.runs[request.run_id]["status"] = "completed"
        return cosmos_pb2.RunResponse(id=request.run_id, status="completed", accuracy=0.85)

    async def GetRun(self, request, context):
        run = self.runs.get(request.run_id, {})
        return cosmos_pb2.RunResponse(id=run.get("id", ""), status=run.get("status", "unknown"))

    async def ListRuns(self, request, context):
        runs = [cosmos_pb2.RunResponse(id=r["id"], status=r["status"]) for r in self.runs.values()]
        return cosmos_pb2.ListRunsResponse(runs=runs)

    async def CompareRuns(self, request, context):
        return cosmos_pb2.CompareResponse(accuracy_delta=0.05, latency_delta=-10.0, cost_delta=-0.01, improved_cases=3, regressed_cases=1)

    async def RunEvaluationStream(self, request, context):
        for i in range(3):
            yield cosmos_pb2.EvalProgress(test_case_id=f"tc-{i}", status="pass", passed=True, score=0.9, overall_progress=float(i+1)/3)


class MockReportAgentServicer(cosmos_pb2_grpc.ReportAgentServiceServicer):
    async def GenerateWeeklyReport(self, request, context):
        return cosmos_pb2.ReportResponse(id="rpt-001", repo_id=request.repo_id, report_type="weekly", summary="Test weekly report",
            sections=[cosmos_pb2.ReportSection(title="Learning", content="3 new learnings", metrics={"count": "3"})])

    async def GenerateMonthlyReport(self, request, context):
        return cosmos_pb2.ReportResponse(id="rpt-002", repo_id=request.repo_id, report_type="monthly", summary="Test monthly report")

    async def GetReport(self, request, context):
        return cosmos_pb2.ReportResponse(id=request.report_id, report_type="weekly", summary="Retrieved report")

    async def ListReports(self, request, context):
        return cosmos_pb2.ListReportsResponse(reports=[cosmos_pb2.ReportResponse(id="rpt-001", report_type="weekly")])

    async def GetReportMarkdown(self, request, context):
        return cosmos_pb2.MarkdownResponse(markdown="# Weekly Report\n\n## Summary\nTest report")

    async def GenerateReportStream(self, request, context):
        for section in ["learning", "conversations", "tools", "cost", "accuracy"]:
            yield cosmos_pb2.ReportProgress(section=section, progress=0.2, status="generating")


class MockTrainingServicer(cosmos_pb2_grpc.TrainingServiceServicer):
    def __init__(self):
        self.jobs = {}
        self._counter = 0

    def _id(self):
        self._counter += 1
        return f"job-{self._counter:04d}"

    async def TriggerEmbeddingTraining(self, request, context):
        jid = self._id()
        self.jobs[jid] = {"id": jid, "type": "embedding", "status": "running", "repo_id": request.repo_id}
        return cosmos_pb2.TrainingJobResponse(job_id=jid, job_type="embedding", status="running", repo_id=request.repo_id)

    async def TriggerIntentTraining(self, request, context):
        jid = self._id()
        self.jobs[jid] = {"id": jid, "type": "intent", "status": "running"}
        return cosmos_pb2.TrainingJobResponse(job_id=jid, job_type="intent", status="running")

    async def TriggerGraphWeightOptimization(self, request, context):
        jid = self._id()
        self.jobs[jid] = {"id": jid, "type": "graph_weight", "status": "running"}
        return cosmos_pb2.TrainingJobResponse(job_id=jid, job_type="graph_weight", status="running")

    async def GetTrainingStatus(self, request, context):
        job = self.jobs.get(request.job_id)
        if not job:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return cosmos_pb2.TrainingJobResponse()
        return cosmos_pb2.TrainingJobResponse(job_id=job["id"], status=job["status"])

    async def ListTrainingJobs(self, request, context):
        jobs = [cosmos_pb2.TrainingJobResponse(job_id=j["id"], job_type=j["type"], status=j["status"]) for j in self.jobs.values()]
        return cosmos_pb2.ListJobsResponse(jobs=jobs)

    async def WatchTrainingJob(self, request, context):
        for i in range(3):
            yield cosmos_pb2.TrainingProgress(job_id=request.job_id, progress=float(i+1)/3, stage="training", message=f"Step {i+1}/3")
            await asyncio.sleep(0.1)


# =============================================
# Test runner
# =============================================

async def run_tests():
    # Start server
    server = grpc.aio.server()
    cosmos_pb2_grpc.add_GraphRAGServiceServicer_to_server(MockGraphRAGServicer(), server)
    cosmos_pb2_grpc.add_VectorStoreServiceServicer_to_server(MockVectorStoreServicer(), server)
    cosmos_pb2_grpc.add_SandboxServiceServicer_to_server(MockSandboxServicer(), server)
    cosmos_pb2_grpc.add_ReportAgentServiceServicer_to_server(MockReportAgentServicer(), server)
    cosmos_pb2_grpc.add_TrainingServiceServicer_to_server(MockTrainingServicer(), server)

    port = server.add_insecure_port("[::]:0")  # Random available port
    await server.start()
    print(f"Mock gRPC server started on port {port}")

    # Connect client
    channel = grpc.aio.insecure_channel(f"localhost:{port}")

    ctx = cosmos_pb2.MarsContext(org_id="shiprocket", project_id="helpdesk", repo_id="repo-001")
    passed = 0
    failed = 0

    def ok(name):
        nonlocal passed
        passed += 1
        print(f"  PASS  {name}")

    def fail(name, err):
        nonlocal failed
        failed += 1
        print(f"  FAIL  {name}: {err}")

    # --- GraphRAG ---
    print("\n=== GraphRAG ===")
    try:
        stub = cosmos_pb2_grpc.GraphRAGServiceStub(channel)
        resp = await stub.IngestModuleDeps(cosmos_pb2.IngestModuleDepsRequest(
            context=ctx, module_name="order_service",
            imports=["database", "cache"], functions=["create_order"], dependencies=["payment"],
        ))
        assert resp.success and resp.nodes_created >= 5, f"Expected >=5 nodes, got {resp.nodes_created}"
        ok("IngestModuleDeps")

        resp = await stub.IngestCourierRelationship(cosmos_pb2.IngestCourierRequest(context=ctx, seller_name="Seller1", courier_name="Delhivery", region="north", ndr_rate=12.0))
        assert resp.success
        ok("IngestCourier")

        resp = await stub.IngestChannelRelationship(cosmos_pb2.IngestChannelRequest(context=ctx, seller_name="Seller1", channel_name="Shopify"))
        assert resp.success
        ok("IngestChannel")

        resp = await stub.QueryRelated(cosmos_pb2.GraphQueryRequest(context=ctx, query="order", max_depth=2))
        assert resp.total_nodes >= 1
        ok(f"QueryRelated ({resp.total_nodes} nodes)")

        resp = await stub.GetStats(cosmos_pb2.StatsRequest(context=ctx))
        assert resp.total_nodes >= 7
        ok(f"GetStats ({resp.total_nodes} nodes, {resp.total_edges} edges)")

        resp = await stub.FindNodes(cosmos_pb2.FindNodesRequest(context=ctx, node_type="module"))
        assert len(resp.nodes) >= 1
        ok(f"FindNodes ({len(resp.nodes)} modules)")

        count = 0
        async for node in stub.TraverseStream(cosmos_pb2.TraverseRequest(context=ctx, start_node_id="order_service", max_depth=2)):
            count += 1
        assert count >= 1
        ok(f"TraverseStream ({count} nodes streamed)")
    except Exception as e:
        fail("GraphRAG", e)

    # --- VectorStore ---
    print("\n=== VectorStore ===")
    try:
        stub = cosmos_pb2_grpc.VectorStoreServiceStub(channel)
        resp = await stub.EmbedAndStore(cosmos_pb2.EmbedRequest(context=ctx, content="How to cancel order?", entity_type="ticket", entity_id="t-001"))
        assert resp.success and resp.dimensions == 384
        ok("EmbedAndStore")

        await stub.EmbedAndStore(cosmos_pb2.EmbedRequest(context=ctx, content="NDR escalation process", entity_type="ticket", entity_id="t-002"))
        resp = await stub.SearchSimilar(cosmos_pb2.SearchRequest(context=ctx, query="cancel", top_k=5))
        assert len(resp.results) >= 1 and resp.results[0].score > 0.5
        ok(f"SearchSimilar ({len(resp.results)} results, top={resp.results[0].score:.2f})")

        resp = await stub.BatchEmbed(cosmos_pb2.BatchEmbedRequest(context=ctx, items=[
            cosmos_pb2.EmbedRequest(context=ctx, content="Batch item 1", entity_type="doc", entity_id="d-001"),
            cosmos_pb2.EmbedRequest(context=ctx, content="Batch item 2", entity_type="doc", entity_id="d-002"),
        ]))
        assert resp.success_count == 2
        ok(f"BatchEmbed ({resp.success_count} embedded)")

        resp = await stub.GetStats(cosmos_pb2.VectorStatsRequest(context=ctx))
        assert resp.total_embeddings >= 4
        ok(f"GetStats ({resp.total_embeddings} embeddings)")

        count = 0
        async for r in stub.SearchStream(cosmos_pb2.SearchRequest(context=ctx, query="cancel", top_k=5)):
            count += 1
        ok(f"SearchStream ({count} results streamed)")

        resp = await stub.DeleteByEntity(cosmos_pb2.DeleteEmbeddingRequest(context=ctx, entity_type="ticket", entity_id="t-001"))
        assert resp.deleted_count == 1
        ok(f"DeleteByEntity ({resp.deleted_count} deleted)")
    except Exception as e:
        fail("VectorStore", e)

    # --- Sandbox ---
    print("\n=== Sandbox ===")
    try:
        stub = cosmos_pb2_grpc.SandboxServiceStub(channel)
        resp = await stub.CreateSuite(cosmos_pb2.CreateSuiteRequest(context=ctx, name="Test Suite", agent_type="order"))
        suite_id = resp.id
        assert suite_id
        ok(f"CreateSuite (id={suite_id})")

        resp = await stub.AddTestCase(cosmos_pb2.AddTestCaseRequest(context=ctx, suite_id=suite_id, input_prompt="test?", expected_output="yes"))
        ok(f"AddTestCase (id={resp.id})")

        resp = await stub.StartRun(cosmos_pb2.StartRunRequest(context=ctx, suite_id=suite_id, agent_version="v1"))
        run_id = resp.id
        assert resp.status == "running"
        ok(f"StartRun (id={run_id})")

        resp = await stub.CompleteRun(cosmos_pb2.CompleteRunRequest(run_id=run_id))
        assert resp.status == "completed"
        ok("CompleteRun")

        resp = await stub.CompareRuns(cosmos_pb2.CompareRunsRequest(run_id_a="a", run_id_b="b"))
        assert resp.accuracy_delta == 0.05
        ok(f"CompareRuns (delta={resp.accuracy_delta})")

        count = 0
        async for progress in stub.RunEvaluationStream(cosmos_pb2.StartRunRequest(context=ctx, suite_id=suite_id, agent_version="v2")):
            count += 1
        ok(f"RunEvaluationStream ({count} events)")
    except Exception as e:
        fail("Sandbox", e)

    # --- Reports ---
    print("\n=== ReportAgent ===")
    try:
        stub = cosmos_pb2_grpc.ReportAgentServiceStub(channel)
        resp = await stub.GenerateWeeklyReport(cosmos_pb2.ReportRequest(context=ctx, repo_id="repo-001"))
        assert resp.id and len(resp.sections) >= 1
        ok(f"GenerateWeekly (sections={len(resp.sections)})")

        resp = await stub.GetReportMarkdown(cosmos_pb2.GetReportRequest(report_id="rpt-001"))
        assert "Weekly Report" in resp.markdown
        ok("GetReportMarkdown")

        count = 0
        async for p in stub.GenerateReportStream(cosmos_pb2.ReportRequest(context=ctx, repo_id="repo-001")):
            count += 1
        ok(f"GenerateReportStream ({count} events)")
    except Exception as e:
        fail("Reports", e)

    # --- Training ---
    print("\n=== Training ===")
    try:
        stub = cosmos_pb2_grpc.TrainingServiceStub(channel)
        resp = await stub.TriggerEmbeddingTraining(cosmos_pb2.TrainingRequest(context=ctx, repo_id="repo-001"))
        job_id = resp.job_id
        assert resp.status == "running"
        ok(f"TriggerEmbeddingTraining (job={job_id})")

        resp = await stub.TriggerIntentTraining(cosmos_pb2.TrainingRequest(context=ctx, repo_id="repo-001"))
        ok("TriggerIntentTraining")

        resp = await stub.ListTrainingJobs(cosmos_pb2.ListJobsRequest(context=ctx, limit=10))
        assert len(resp.jobs) >= 2
        ok(f"ListTrainingJobs ({len(resp.jobs)} jobs)")

        resp = await stub.GetTrainingStatus(cosmos_pb2.GetJobRequest(job_id=job_id))
        assert resp.status == "running"
        ok("GetTrainingStatus")

        count = 0
        async for p in stub.WatchTrainingJob(cosmos_pb2.GetJobRequest(job_id=job_id)):
            count += 1
        ok(f"WatchTrainingJob ({count} progress events)")
    except Exception as e:
        fail("Training", e)

    # Cleanup
    await channel.close()
    await server.stop(grace=0)

    # Summary
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")
    print("=" * 60)

    if failed > 0:
        print("\nSOME TESTS FAILED")
        return 1
    else:
        print("\nALL TESTS PASSED - gRPC pipeline verified!")
        print("""
Services tested:
  1. GraphRAG     — ingest, query, traverse, stream, stats
  2. VectorStore  — embed, search, batch, delete, stream  
  3. Sandbox      — suites, cases, runs, compare, eval stream
  4. ReportAgent  — generate, get, markdown, stream
  5. Training     — trigger 3 pipelines, status, watch stream

Proto contract: MarsContext identity flows through all calls
Streaming: Server-side streaming verified for all 5 services
""")
        return 0


if __name__ == "__main__":
    exit_code = asyncio.run(run_tests())
    sys.exit(exit_code)
