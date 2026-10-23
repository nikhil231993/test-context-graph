"""Regression tests for the package 10 review fixes in context.py, each on a small made-up repo."""

from __future__ import annotations

from pathlib import Path

from test_context_graph.context import MAX_COLLABORATORS, MAX_ENDPOINTS, ContextEngine
from test_context_graph.embeddings import HashEmbedder
from test_context_graph.indexer import index_repo

MAIN = "src/main/java/org/shop"
TEST = "src/test/java/org/shop"


def engine(tmp_path: Path, files: dict[str, str]) -> ContextEngine:
    repo = tmp_path / "repo"
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("package org.shop;\n" + text)
    index_repo(repo, tmp_path / "index", HashEmbedder())
    return ContextEngine(tmp_path / "index")


def test_an_inherited_own_call_is_not_a_call_on_a_dependency_sharing_the_supertype(tmp_path):
    e = engine(tmp_path, {
        f"{MAIN}/BaseService.java": "public abstract class BaseService { protected void audit(String m) {} }",
        f"{MAIN}/AuditService.java": "@Service public class AuditService extends BaseService { public void record() {} }",
        f"{MAIN}/OrderService.java": """
@Service
public class OrderService extends BaseService {
    private final AuditService auditService;
    public OrderService(AuditService auditService) { this.auditService = auditService; }
    public void place() {
        audit("placing");
        auditService.record();
    }
}""",
    })  # fmt: skip
    deps = e.test_context("OrderService#place", max_docs=0)["dependencies"]
    (audit,) = [d for d in deps if d["id"] == "type:org.shop.AuditService"]
    assert [c["label"] for c in audit["calls"]] == ["AuditService#record()"]


def test_a_plain_class_constructor_parameter_is_data_not_a_bean(tmp_path):
    e = engine(tmp_path, {
        f"{MAIN}/Owner.java": "public class Owner { private String name; }",
        f"{MAIN}/OwnerSavedEvent.java": """
public class OwnerSavedEvent {
    private final Owner owner;
    public OwnerSavedEvent(Owner owner) { this.owner = owner; }
}""",
        f"{MAIN}/OwnerService.java": """
@Service
public class OwnerService {
    public OwnerSavedEvent save(Owner owner) { return new OwnerSavedEvent(owner); }
}""",
    })  # fmt: skip
    names = [d["name"] for d in e.test_context("OwnerService#save", max_docs=0)["data_types"]]
    assert "Owner" in names and "OwnerSavedEvent" in names


def test_a_component_constructor_parameter_is_still_a_bean(tmp_path):
    e = engine(tmp_path, {
        f"{MAIN}/Clock.java": "public class Clock { public long now() { return 0; } }",
        f"{MAIN}/Stamper.java": """
@Component
public class Stamper {
    private final Clock clock;
    public Stamper(Clock clock) { this.clock = clock; }
}""",
        f"{MAIN}/StampService.java": """
@Service
public class StampService {
    public Stamper stamper(Clock clock) { return new Stamper(clock); }
}""",
    })  # fmt: skip
    names = [d["name"] for d in e.test_context("StampService#stamper", max_docs=0)["data_types"]]
    assert "Clock" not in names  # injected into a @Component: a bean


def test_left_out_calls_and_endpoints_are_noted(tmp_path):
    calls = MAX_COLLABORATORS + 2
    endpoints = MAX_ENDPOINTS + 1
    util = "".join(f"    public static void m{i}() {{}}\n" for i in range(calls))
    body = "".join(f"        Util.m{i}();\n" for i in range(calls))
    mappings = "".join(
        f'    @GetMapping("/items/{i}") public String get{i}() {{ return ""; }}\n' for i in range(endpoints)
    )
    e = engine(tmp_path, {
        f"{MAIN}/Util.java": f"public class Util {{\n{util}}}",
        f"{MAIN}/Busy.java": f"@Service public class Busy {{\n    public void run() {{\n{body}    }}\n}}",
        f"{MAIN}/ItemController.java": f"@RestController public class ItemController {{\n{mappings}}}",
    })  # fmt: skip
    busy = e.test_context("Busy#run", max_docs=0)
    assert len(busy["collaborators"]) == MAX_COLLABORATORS
    assert "2 more calls left out" in busy["notes"]
    controller = e.test_context("ItemController", max_docs=0)
    assert len(controller["endpoints"]) == MAX_ENDPOINTS
    assert "1 more endpoint left out" in controller["notes"]


def test_a_test_class_target_is_its_own_suggested_test_file(tmp_path):
    e = engine(tmp_path, {
        f"{MAIN}/OrderService.java": "@Service public class OrderService { public void place() {} }",
        f"{TEST}/OrderServiceTests.java": """
class OrderServiceTests {
    @Test void places() { new OrderService().place(); }
}""",
    })  # fmt: skip
    conventions = e.test_context("OrderServiceTests", max_docs=0)["conventions"]
    assert conventions["suggested_test_file"] == f"{TEST}/OrderServiceTests.java"
    assert "is a test class" in conventions["advice"]
