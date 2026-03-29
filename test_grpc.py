"""End-to-end gRPC test for COSMOS services.

Usage:
    # Terminal 1: Start COSMOS
    cd cosmos && source .venv/bin/activate && uvicorn app.main:app --port 8000

    # Terminal 2: Run tests
    cd cosmos && source .venv/bin/activate && python test_grpc.py
"""

import asyncio
import grpc
import sys
import json

# Add parent to path for imports
sys.path.insert(0, ".")
from app.grpc_gen import cosmos_pb2, cosmos_pb2_grpc


async def test_graphrag(channel):
    """Test GraphRAG service via gRPC."""
    stub = cosmos_pb2_grpc.GraphRAGServiceStub(channel)

    print("\n=== GraphRAG Tests ===")

    # 1. Ingest module dependencies
    ctx = cosmos_pb2.MarsContext(
        org_id="shiprocket", project_id="helpdesk",
        repo_id="repo-001", entity_type="module",
        entity_id="test", action="ingest",
    )
    resp = await stub.IngestModuleDeps(cosmos_pb2.IngestModuleDepsRequest(
        context=ctx, module_name="order_service",
        imports=["database", "cache", "logger"],
        functions=["create_order", "cancel_order", "get_order"],
        dependencies=["payment_service", "inventory_service"],
    ))
    print(f"  [PASS] IngestModuleDeps: {resp.message} (nodes={resp.nodes_created}, edges={resp.edges_created})")

    # 2. Ingest courier relationship
    resp = await stub.IngestCourierRelationship(cosmos_pb2.IngestCourierRequest(
        context=ctx, seller_name="TestSeller",
        courier_name="Delhivery", region="north", ndr_rate=12.5,
    ))
    print(f"  [PASS] IngestCourier: {resp.message}")

    # 3. Ingest channel relationship
    resp = await stub.IngestChannelRelationship(cosmos_pb2.IngestChannelRequest(
        context=ctx, seller_name="TestSeller", channel_name="Shopify",
    ))
    print(f"  [PASS] IngestChannel: {resp.message}")

    # 4. Query related entities
    resp = await stub.QueryRelated(cosmos_pb2.GraphQueryRequest(
        context=ctx, query="order_service", max_depth=2,
    ))
    print(f"  [PASS] QueryRelated: {resp.total_nodes} nodes, {resp.total_edges} edges")

    # 5. Get stats
    resp = await stub.GetStats(cosmos_pb2.StatsRequest(context=ctx))
    print(f"  [PASS] GetStats: {resp.total_nodes} nodes, {resp.total_edges} edges")

    # 6. Find nodes
    resp = await stub.FindNodes(cosmos_pb2.FindNodesRequest(
        context=ctx, node_type="module", repo_id="repo-001",
    ))
    print(f"  [PASS] FindNodes: {len(resp.nodes)} module nodes found")

    # 7. Test streaming traverse
    print("  Testing TraverseStream...")
    count = 0
    async for node in stub.TraverseStream(cosmos_pb2.TraverseRequest(
        context=ctx, start_node_id="", max_depth=2,
    )):
        count += 1
    print(f"  [PASS] TraverseStream: {count} nodes streamed")

    return True


async def test_vectorstore(channel):
    """Test VectorStore service via gRPC."""
    stub = cosmos_pb2_grpc.VectorStoreServiceStub(channel)

    print("\n=== VectorStore Tests ===")

    ctx = cosmos_pb2.MarsContext(
        org_id="shiprocket", project_id="helpdesk",
        repo_id="repo-001", entity_type="ticket",
        entity_id="ticket-001", action="embed",
    )

    # 1. Embed and store
    resp = await stub.EmbedAndStore(cosmos_pb2.EmbedRequest(
        context=ctx, content="How to cancel an order in Shiprocket?",
        entity_type="ticket", entity_id="ticket-001",
        metadata={"priority": "high"},
    ))
    print(f"  [PASS] EmbedAndStore: success={resp.success}, dims={resp.dimensions}")

    # 2. Embed more
    await stub.EmbedAndStore(cosmos_pb2.EmbedRequest(
        context=ctx, content="NDR escalation process for Delhivery",
        entity_type="ticket", entity_id="ticket-002",
    ))

    # 3. Search similar
    resp = await stub.SearchSimilar(cosmos_pb2.SearchRequest(
        context=ctx, query="cancel order", repo_id="repo-001", top_k=5,
    ))
    print(f"  [PASS] SearchSimilar: {len(resp.results)} results")
    for r in resp.results[:2]:
        print(f"    - [{r.score:.3f}] {r.content[:60]}...")

    # 4. Stats
    resp = await stub.GetStats(cosmos_pb2.VectorStatsRequest(context=ctx, repo_id="repo-001"))
    print(f"  [PASS] GetStats: {resp.total_embeddings} embeddings, {resp.dimensions}d")

    return True


async def test_sandbox(channel):
    """Test Sandbox service via gRPC."""
    stub = cosmos_pb2_grpc.SandboxServiceStub(channel)

    print("\n=== Sandbox Tests ===")

    ctx = cosmos_pb2.MarsContext(
        org_id="shiprocket", project_id="helpdesk",
        repo_id="repo-001", entity_type="sandbox",
        entity_id="", action="test",
    )

    # 1. Create suite
    resp = await stub.CreateSuite(cosmos_pb2.CreateSuiteRequest(
        context=ctx, name="Order Agent v1 Tests",
        description="Test order handling agent", agent_type="order_agent", repo_id="repo-001",
    ))
    suite_id = resp.id
    print(f"  [PASS] CreateSuite: id={suite_id[:8]}...")

    # 2. Add test case
    resp = await stub.AddTestCase(cosmos_pb2.AddTestCaseRequest(
        context=ctx, suite_id=suite_id,
        input_prompt="What is the status of order 12345?",
        expected_output="Order 12345 is in transit",
        expected_tools=["get_order_status"],
        category="order_query", difficulty="easy",
    ))
    print(f"  [PASS] AddTestCase: id={resp.id[:8]}...")

    # 3. List suites
    resp = await stub.ListSuites(cosmos_pb2.ListSuitesRequest(context=ctx, agent_type="order_agent"))
    print(f"  [PASS] ListSuites: {len(resp.suites)} suites")

    # 4. Start run
    resp = await stub.StartRun(cosmos_pb2.StartRunRequest(
        context=ctx, suite_id=suite_id, agent_version="v1.0",
    ))
    run_id = resp.id
    print(f"  [PASS] StartRun: id={run_id[:8]}..., status={resp.status}")

    return True


async def test_training(channel):
    """Test Training service via gRPC."""
    stub = cosmos_pb2_grpc.TrainingServiceStub(channel)

    print("\n=== Training Tests ===")

    ctx = cosmos_pb2.MarsContext(
        org_id="shiprocket", project_id="helpdesk",
        repo_id="repo-001", action="train",
    )

    # 1. Trigger embedding training
    resp = await stub.TriggerEmbeddingTraining(cosmos_pb2.TrainingRequest(
        context=ctx, repo_id="repo-001",
    ))
    print(f"  [PASS] TriggerEmbeddingTraining: job_id={resp.job_id[:8]}..., status={resp.status}")

    # 2. List jobs
    resp = await stub.ListTrainingJobs(cosmos_pb2.ListJobsRequest(context=ctx, limit=5))
    print(f"  [PASS] ListTrainingJobs: {len(resp.jobs)} jobs")

    return True


async def test_reports(channel):
    """Test ReportAgent service via gRPC."""
    stub = cosmos_pb2_grpc.ReportAgentServiceStub(channel)

    print("\n=== ReportAgent Tests ===")

    ctx = cosmos_pb2.MarsContext(
        org_id="shiprocket", project_id="helpdesk",
        repo_id="repo-001", action="report",
    )

    # 1. Generate weekly report
    resp = await stub.GenerateWeeklyReport(cosmos_pb2.ReportRequest(
        context=ctx, repo_id="repo-001",
    ))
    report_id = resp.id
    print(f"  [PASS] GenerateWeeklyReport: id={report_id[:8] if report_id else 'N/A'}..., sections={len(resp.sections)}")

    # 2. List reports
    resp = await stub.ListReports(cosmos_pb2.ListReportsRequest(
        context=ctx, report_type="weekly", limit=5,
    ))
    print(f"  [PASS] ListReports: {len(resp.reports)} reports")

    # 3. Stream report generation progress
    print("  Testing GenerateReportStream...")
    count = 0
    async for progress in stub.GenerateReportStream(cosmos_pb2.ReportRequest(context=ctx, repo_id="repo-001")):
        count += 1
    print(f"  [PASS] GenerateReportStream: {count} progress events")

    return True


async def main():
    print("=" * 60)
    print("COSMOS gRPC Integration Test")
    print("=" * 60)
    print(f"Connecting to localhost:50051...")

    try:
        channel = grpc.aio.insecure_channel(
            "localhost:50051",
            options=[
                ("grpc.max_send_message_length", 50 * 1024 * 1024),
                ("grpc.max_receive_message_length", 50 * 1024 * 1024),
            ],
        )
        # Check connectivity
        await channel.channel_ready()
        print("[OK] Connected to gRPC server\n")
    except Exception as e:
        print(f"[FAIL] Cannot connect to gRPC server: {e}")
        print("\nMake sure COSMOS is running:")
        print("  cd cosmos && source .venv/bin/activate && uvicorn app.main:app --port 8000")
        sys.exit(1)

    results = {}
    for name, test_fn in [
        ("GraphRAG", test_graphrag),
        ("VectorStore", test_vectorstore),
        ("Sandbox", test_sandbox),
        ("Training", test_training),
        ("Reports", test_reports),
    ]:
        try:
            ok = await test_fn(channel)
            results[name] = "PASS" if ok else "FAIL"
        except grpc.aio.AioRpcError as e:
            print(f"  [FAIL] {name}: gRPC error: {e.code()} — {e.details()}")
            results[name] = f"FAIL ({e.code().name})"
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            results[name] = f"FAIL ({type(e).__name__})"

    await channel.close()

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    all_pass = True
    for name, result in results.items():
        status = "✓" if result == "PASS" else "✗"
        print(f"  {status} {name}: {result}")
        if result != "PASS":
            all_pass = False

    print(f"\nOverall: {'ALL PASSED' if all_pass else 'SOME FAILED'}")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(main())
