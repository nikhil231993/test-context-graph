"""graph.build_graph on a small made-up Spring codebase (a garden shop), an OpenAPI spec and a
readme. The Java is parsed with parse_java; the spec and the doc sections are built directly."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from test_context_graph.graph import (
    TypeIndex,
    build_graph,
    match_url,
    project_prefix,
    type_role,
)
from test_context_graph.java_parser import parse_java
from test_context_graph.models import (
    ApiOperation,
    ApiParameter,
    ApiSchema,
    ApiSpec,
    DocSection,
    Edge,
    Graph,
    Node,
    TypeDecl,
    to_dict,
)

G = "org.acme.garden"
MAIN = "src/main/java/org/acme/garden"
TEST = "src/test/java/org/acme/garden"
SPEC_PATH = "src/main/resources/garden.yml"

JAVA = {
    f"{MAIN}/GardenApplication.java": """
package org.acme.garden;

@SpringBootApplication
public class GardenApplication {
    public static void main(String[] args) {
        SpringApplication.run(GardenApplication.class, args);
    }
}
""",
    f"{MAIN}/model/BaseEntity.java": """
package org.acme.garden.model;

@MappedSuperclass
public abstract class BaseEntity {
    @Id
    protected Integer id;

    public Integer getId() {
        return id;
    }

    public boolean isNew() {
        return this.id == null;
    }
}
""",
    f"{MAIN}/model/Plant.java": """
package org.acme.garden.model;

import java.util.ArrayList;
import java.util.List;

/**
 * A plant in the garden.
 */
@Entity
public class Plant extends BaseEntity {
    private String name;

    private final List<Tag> tags = new ArrayList<>();

    public Plant() {
    }

    public Plant(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }

    public void addTag(String label) {
        Tag tag = new Tag(label);
        tags.add(tag);
        tag.attach(this);
    }

    public String describe() {
        return name + " (" + tags.size() + " tags)";
    }

    public class Tag {
        private final String label;

        public Tag(String label) {
            this.label = label;
        }

        void attach(Plant plant) {
            plant.describe();
            getName();
        }
    }
}
""",
    f"{MAIN}/repository/PlantRepository.java": """
package org.acme.garden.repository;

import java.util.List;
import org.acme.garden.model.Plant;
import org.springframework.data.jpa.repository.JpaRepository;

public interface PlantRepository extends JpaRepository<Plant, Integer> {
    List<Plant> findByName(String name);

    List<Plant> findByName(String name, int limit);
}
""",
    f"{MAIN}/repository/SeedStore.java": """
package org.acme.garden.repository;

@Repository
public class SeedStore {
    public int countSeeds(String plantName) {
        return plantName.length();
    }
}
""",
    f"{MAIN}/repository/SeedRecords.java": """
package org.acme.garden.repository;

import org.springframework.data.repository.CrudRepository;

public interface SeedRecords extends CrudRepository<Object, Integer> {
}
""",
    f"{MAIN}/service/Finder.java": """
package org.acme.garden.service;

public interface Finder<T> {
    T findOne(Integer id);

    void store(T item);
}
""",
    f"{MAIN}/service/PlantService.java": """
package org.acme.garden.service;

import java.util.Collection;
import org.acme.garden.model.Plant;

public interface PlantService extends Finder<Plant> {
    /** Finds one plant. */
    Plant findPlant(int id);

    Collection<Plant> findAll();

    void savePlant(Plant plant);
}
""",
    f"{MAIN}/service/AuditedService.java": """
package org.acme.garden.service;

public abstract class AuditedService {
    protected AuditedService(String area) {
    }

    protected void audit(String message) {
    }
}
""",
    f"{MAIN}/service/AuditLog.java": """
package org.acme.garden.service;

@Component
public class AuditLog {
    public void record(String entry) {
    }
}
""",
    f"{MAIN}/service/PlantServiceImpl.java": """
package org.acme.garden.service;

import java.util.Collection;
import java.util.List;
import org.acme.garden.model.*;
import org.acme.garden.repository.PlantRepository;
import org.acme.garden.repository.SeedStore;
import org.acme.garden.util.GardenUtils;

@Service
public class PlantServiceImpl extends AuditedService implements PlantService {
    private final PlantRepository plantRepository;
    private final SeedStore seedStore;
    @Autowired
    private Clock clock;
    @Inject
    private GardenUtils utils;
    private AuditLog auditLog;

    public PlantServiceImpl(PlantRepository plantRepository, SeedStore seedStore) {
        super("plants");
        this.plantRepository = plantRepository;
        this.seedStore = seedStore;
    }

    @Autowired
    public void setAuditLog(AuditLog auditLog) {
        this.auditLog = auditLog;
    }

    @Override
    public Plant findPlant(int id) {
        audit("find");
        return plantRepository.findById(id).orElseThrow();
    }

    @Override
    public Collection<Plant> findAll() {
        List<Plant> some = plantRepository.findByName("fern");
        List<Plant> few = plantRepository.findByName("fern", 2);
        some.forEach(this::water);
        return few;
    }

    @Override
    public void savePlant(Plant plant) {
        String clean = GardenUtils.normalise(plant.getName());
        Plant copy = new Plant(clean);
        findPlant(1).describe();
        this.water(copy);
        auditLog.record(clean);
        auditLog.record(clean + "!");
    }

    public void savePlant(String name) {
        savePlant(new Plant(name));
    }

    @Override
    public Plant findOne(Integer id) {
        return findPlant(id);
    }

    @Override
    public void store(Plant item) {
        savePlant(item);
    }

    @Override
    protected void audit(String message) {
        super.audit(message);
    }

    private void water(Plant plant) {
        seedStore.countSeeds(plant.getName());
    }
}
""",
    f"{MAIN}/mapper/PlantMapper.java": """
package org.acme.garden.mapper;

import java.util.List;
import org.acme.garden.model.Plant;
import org.acme.garden.rest.dto.PlantDto;
import org.acme.garden.rest.dto.PlantFieldsDto;

@Mapper
public interface PlantMapper {
    PlantDto toPlantDto(Plant plant);

    List<PlantDto> toPlantDtos(List<Plant> plants);

    Plant toPlant(PlantFieldsDto fields);
}
""",
    f"{MAIN}/web/PlantController.java": """
package org.acme.garden.web;

import java.util.List;
import org.acme.garden.mapper.PlantMapper;
import org.acme.garden.model.Plant;
import org.acme.garden.rest.api.PlantsApi;
import org.acme.garden.rest.dto.PlantDto;
import org.acme.garden.service.PlantService;
import org.springframework.http.ResponseEntity;

@RestController
@RequestMapping("api")
public class PlantController implements PlantsApi {
    private final PlantService plantService;
    private final PlantMapper plantMapper;

    public PlantController(PlantService plantService, PlantMapper plantMapper) {
        this.plantService = plantService;
        this.plantMapper = plantMapper;
    }

    @Override
    public ResponseEntity<List<PlantDto>> listPlants() {
        return ResponseEntity.ok(plantMapper.toPlantDtos(List.copyOf(plantService.findAll())));
    }

    @Override
    public ResponseEntity<PlantDto> getPlant(Integer plantId) {
        Plant plant = plantService.findPlant(plantId);
        return ResponseEntity.ok(plantMapper.toPlantDto(plant));
    }

    @Override
    public ResponseEntity<Void> tagPlant(Integer plantId, String tag) {
        plantService.findPlant(plantId).addTag(tag);
        return ResponseEntity.noContent().build();
    }

    public String getSeed() {
        return "named like an operation of another tag";
    }
}
""",
    f"{MAIN}/web/SeedController.java": """
package org.acme.garden.web;

import org.acme.garden.repository.SeedStore;

@RestController
@RequestMapping({"/api"})
public class SeedController {
    private final SeedStore seedStore;

    SeedController(SeedStore seedStore) {
        this.seedStore = seedStore;
    }

    @GetMapping("/seeds/{id}")
    public int fetchSeed(@PathVariable int id) {
        return seedStore.countSeeds("any");
    }

    @DeleteMapping(path = "/seeds/{id}")
    public void deleteSeed(@PathVariable int id) {
    }

    @RequestMapping(value = "/seeds/stats", method = RequestMethod.GET)
    public String stats() {
        return "ok";
    }

    @RequestMapping("/ping")
    public String ping() {
        return "pong";
    }
}
""",
    f"{MAIN}/web/GardenAdvice.java": """
package org.acme.garden.web;

@RestControllerAdvice
public class GardenAdvice {
}
""",
    f"{MAIN}/config/WebConfig.java": """
package org.acme.garden.config;

@Configuration
@Profile({"web", "!batch"})
public class WebConfig {
}
""",
    f"{MAIN}/util/GardenUtils.java": """
package org.acme.garden.util;

public final class GardenUtils {
    public static String normalise(String name) {
        return name.trim();
    }
}
""",
    f"{TEST}/web/PlantControllerTests.java": """
package org.acme.garden.web;

import static org.mockito.BDDMockito.given;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;

import org.acme.garden.mapper.PlantMapper;
import org.acme.garden.mapper.PlantMapperImpl;
import org.acme.garden.model.Plant;
import org.acme.garden.rest.dto.PlantDto;
import org.acme.garden.service.PlantService;
import org.springframework.http.HttpMethod;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
class PlantControllerTests {
    @Autowired
    private PlantController plantController;

    @MockitoBean
    private PlantService plantService;

    private final PlantMapper plantMapper = new PlantMapperImpl();

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() throws Exception {
        mockMvc = MockMvcBuilders.standaloneSetup(plantController).build();
        mockMvc.perform(get("/api/plants/7"));
    }

    @Test
    void getsPlant() throws Exception {
        given(plantService.findPlant(1)).willReturn(new Plant("fern"));
        mockMvc.perform(get("/api/plants/1?verbose=true")).andExpect(status().isOk());
    }

    @Test
    void tagsPlant() throws Exception {
        int id = 1;
        mockMvc.perform(put("/api/plants/" + id + "/tags/green"));
    }

    @Test
    void mapsPlant() {
        PlantDto dto = plantMapper.toPlantDto(new Plant("rose"));
        dto.setName("rose");
    }

    @Test
    void listsPlantsAndSeeds() throws Exception {
        mockMvc.perform(get("/api/plants"));
        mockMvc.perform(request(HttpMethod.GET, "/api/seeds/stats"));
        mockMvc.perform(delete("/api/seeds/5"));
    }

    @Test
    void callsTheClientUrl() throws Exception {
        mockMvc.perform(get("/garden/api/plants/3"));
    }

    @Test
    void callsAnotherPrefix() throws Exception {
        mockMvc.perform(get("http://localhost:8080/v9/plants/{id}", 3));
    }
}
""",
    f"{TEST}/web/SeedControllerWebTests.java": """
package org.acme.garden.web;

import static org.mockito.Mockito.mock;

import org.acme.garden.repository.SeedStore;
import org.acme.garden.util.GardenUtils;

@WebMvcTest(controllers = SeedController.class)
class SeedControllerWebTests {
    @MockBean
    private SeedStore seedStore;

    private final GardenUtils utils = mock(GardenUtils.class);

    @Test
    void countsSeeds() {
        seedStore.countSeeds("fern");
    }
}
""",
    f"{TEST}/service/AbstractPlantServiceTests.java": """
package org.acme.garden.service;

import org.acme.garden.model.Plant;

abstract class AbstractPlantServiceTests {
    @Autowired
    protected PlantService plantService;

    @Test
    void findsPlant() {
        Plant plant = plantService.findPlant(1);
        plant.describe();
    }
}
""",
    f"{TEST}/service/PlantServiceJdbcTests.java": """
package org.acme.garden.service;

@ActiveProfiles("jdbc")
class PlantServiceJdbcTests extends AbstractPlantServiceTests {
}
""",
    f"{TEST}/service/PlantServiceImplTests.java": """
package org.acme.garden.service;

import org.acme.garden.repository.PlantRepository;

@ExtendWith(MockitoExtension.class)
class PlantServiceImplTests {
    @Mock
    private PlantRepository plantRepository;

    @InjectMocks
    private PlantServiceImpl service;

    @Test
    void savesPlant() {
        service.savePlant("fern");
        plantRepository.findByName("fern");
    }
}
""",
    f"{TEST}/GardenFlowTests.java": """
package org.acme.garden;

import org.acme.garden.repository.SeedStore;
import org.acme.garden.service.PlantService;

@SpringBootTest
class GardenFlowTests {
    @Autowired
    private PlantService plantService;

    private final SeedStore store = new SeedStore();

    @Test
    void runs() {
    }
}
""",
    f"{TEST}/support/TestPlants.java": """
package org.acme.garden.support;

import org.acme.garden.model.Plant;

public class TestPlants {
    public static Plant fern() {
        return new Plant("fern");
    }
}
""",
    "src/test/java/org/acme/checks/GardenUtilsTest.java": """
package org.acme.checks;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.acme.garden.util.GardenUtils;

class GardenUtilsTest {
    @Test
    void trims() {
        assertEquals("a", GardenUtils.normalise(" a "));
    }
}
""",
}

SPEC = ApiSpec(
    path=SPEC_PATH,
    title="Garden",
    version="1.0",
    base_path="/garden/api",
    operations=[
        ApiOperation(
            "GET", "/plants", operation_id="listPlants", summary="List plants", tags=["plants"],
            responses={"200": "array<Plant>"}, line=10,
        ),
        ApiOperation(
            "POST", "/plants", operation_id="addPlant", tags=["plants"],
            request_body="PlantFields", responses={"201": "Plant", "400": "ProblemDetail"},
            line=20,
        ),
        ApiOperation(
            "GET", "/plants/{plantId}", operation_id="getPlant", summary="Get a plant",
            description="One plant, by id.", tags=["plants"],
            parameters=[ApiParameter("plantId", "path", True, "integer")],
            responses={"200": "Plant", "304": "Not modified.", "404": "ProblemDetail"}, line=30,
        ),
        ApiOperation(
            "PUT", "/plants/{plantId}/tags/{tag}", operation_id="tagPlant", tags=["plants"],
            parameters=[
                ApiParameter("plantId", "path", True, "integer"),
                ApiParameter("tag", "path", True, "string"),
            ],
            responses={"204": "Tagged."}, line=40,
        ),
        ApiOperation(
            "GET", "/seeds/{seedId}", operation_id="getSeed", tags=["seeds"],
            parameters=[ApiParameter("seedId", "path", True, "integer")],
            responses={"200": "Seed"}, line=50,
        ),
        ApiOperation(
            "GET", "/oops", operation_id="failingRequest", tags=["failing"],
            responses={"200": "string"}, line=60,
        ),
    ],
    schemas=[
        ApiSchema("PlantFields", 100, {"name": "string", "labels": "array<string>"}, ["name"],
                  text="PlantFields:\n  type: object\n"),
        ApiSchema("Plant", 110, {"id": "integer", "seeds": "array<Seed>"}, ["id"],
                  ["PlantFields"], text="Plant:\n  allOf:\n  - $ref: '#/components/schemas/PlantFields'\n"),
        ApiSchema("Seed", 120, {"id": "integer"}, ["id"], text="Seed:\n  type: object\n"),
        ApiSchema("ProblemDetail", 130, {"title": "string"}, text="ProblemDetail:\n  type: object\n"),
    ],
)  # fmt: skip

DOCS = [
    DocSection("README.md", "", "", 0, 1, 2, "Made-up garden shop.\n"),
    DocSection(
        "README.md", "Garden", "garden", 1, 3, 7,
        "# Garden\n\nThe PlantService finds plants and PlantController serves `/api/plants/{id}`.\n"
        "GardenUtils trims names; BaseEntity holds ids. Plant is too plain to find.\n"
        "PlantServiceImplX is not PlantServiceImpl.\n",
    ),
    DocSection("README.md", "Empty", "empty", 2, 8, 8, "## Empty\n", ["Garden"]),
    DocSection(
        "README.md", "Client", "client", 2, 9, 12,
        "## Client\n\nClients call /garden/api/seeds/{seedId}.\n| POST | /plants | add one |\n",
        ["Garden"],
    ),
]  # fmt: skip


def t(name: str) -> str:
    return f"type:{G}.{name}"


def m(type_name: str, signature: str) -> str:
    return f"method:{G}.{type_name}#{signature}"


def ep(name: str) -> str:
    return f"endpoint:{name}"


def line_of(path: str, snippet: str, after: int = 0) -> int:
    """The number of the first line of JAVA[path], after line `after`, that has the snippet."""
    for number, line in enumerate(JAVA[path].split("\n"), 1):
        if number > after and snippet in line:
            return number
    raise AssertionError(f"{snippet!r} not in {path}")


def parse_all(sources: dict[str, str]) -> list:
    return [parse_java(text, path) for path, text in sources.items()]


class View:
    """A graph with lookups for the assertions."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.nodes = {n.id: n for n in graph.nodes}
        self.edges = {(e.kind, e.source, e.target): e for e in graph.edges}

    def node(self, node_id: str) -> Node:
        assert node_id in self.nodes, f"no node {node_id}"
        return self.nodes[node_id]

    def edge(self, kind: str, source: str, target: str) -> Edge:
        key = (kind, source, target)
        assert key in self.edges, f"no {kind} {source} -> {target}; {kind} from it: {self.out(kind, source)}"
        return self.edges[key]

    def has(self, kind: str, source: str, target: str) -> bool:
        return (kind, source, target) in self.edges

    def out(self, kind: str, source: str) -> set[str]:
        return {e.target for e in self.graph.edges if e.kind == kind and e.source == source}

    def into(self, kind: str, target: str) -> set[str]:
        return {e.source for e in self.graph.edges if e.kind == kind and e.target == target}


@pytest.fixture(scope="module")
def java_files() -> list:
    return parse_all(JAVA)


@pytest.fixture(scope="module")
def g(java_files) -> View:
    for jf in java_files:
        assert not jf.errors, (jf.path, jf.errors)
    return View(build_graph(java_files, [SPEC], DOCS))


# ---- nodes ---------------------------------------------------------------------------------


def test_every_node_kind_is_built(g):
    kinds = Counter(n.kind for n in g.graph.nodes)
    assert set(kinds) == {"file", "type", "method", "endpoint", "schema", "doc"}


def test_file_nodes_for_java_spec_and_doc_files(g):
    plant = g.node(f"file:{MAIN}/model/Plant.java")
    assert plant.name == "Plant.java" and plant.path == f"{MAIN}/model/Plant.java"
    assert plant.attrs == {"language": "java", "source_set": "main", "package": f"{G}.model"}
    assert g.node(f"file:{TEST}/GardenFlowTests.java").attrs["source_set"] == "test"
    spec = g.node(f"file:{SPEC_PATH}")
    assert spec.attrs["language"] == "openapi" and spec.attrs["source_set"] == "main"
    readme = g.node("file:README.md")
    assert readme.attrs == {"language": "markdown", "source_set": "other"}


def test_type_node_attrs(g):
    plant = g.node(t("model.Plant"))
    assert plant.name == "Plant"
    assert plant.path == f"{MAIN}/model/Plant.java"
    path = f"{MAIN}/model/Plant.java"
    assert plant.start_line == line_of(path, "@Entity")  # annotations included, javadoc not
    assert plant.end_line == len(JAVA[path].rstrip("\n").split("\n"))
    a = plant.attrs
    assert a["type_kind"] == "class" and a["qualified_name"] == f"{G}.model.Plant"
    assert a["package"] == f"{G}.model" and a["source_set"] == "main" and a["role"] == "entity"
    assert a["annotations"] == ["Entity"] and a["modifiers"] == ["public"]
    assert a["extends"] == ["BaseEntity"] and a["implements"] == []
    assert a["fields"] == [
        {"name": "name", "type": "String", "annotations": []},
        {"name": "tags", "type": "List<Tag>", "annotations": []},
    ]
    assert a["is_abstract"] is False and a["external"] is False and a["generated"] is False
    assert a["javadoc"] == "A plant in the garden."
    assert g.node(t("model.BaseEntity")).attrs["is_abstract"] is True
    assert g.node(t("service.Finder")).attrs["type_parameters"] == ["T"]
    assert g.node(t("config.WebConfig")).attrs["profiles"] == ["web", "!batch"]
    assert g.node(t("service.PlantServiceJdbcTests")).attrs["active_profiles"] == ["jdbc"]
    impl = g.node(t("service.PlantServiceImpl")).attrs
    assert impl["extends"] == ["AuditedService"] and impl["implements"] == ["PlantService"]
    assert {"name": "utils", "type": "GardenUtils", "annotations": ["Inject"]} in impl["fields"]


def test_nested_type_node(g):
    tag = g.node(t("model.Plant.Tag"))
    assert tag.name == "Plant.Tag"
    assert tag.attrs["outer"] == f"{G}.model.Plant"


def test_roles(g):
    roles = {
        "GardenApplication": "config",
        "model.BaseEntity": "entity",
        "model.Plant": "entity",
        "repository.PlantRepository": "repository",  # name, and extends JpaRepository
        "repository.SeedStore": "repository",  # @Repository
        "repository.SeedRecords": "repository",  # extends CrudRepository
        "service.PlantServiceImpl": "service",
        "service.AuditLog": "other",  # @Component
        "util.GardenUtils": "other",
        "mapper.PlantMapper": "mapper",
        "web.PlantController": "controller",
        "web.GardenAdvice": "controller_advice",
        "config.WebConfig": "config",
        "web.PlantControllerTests": "test",
        "service.AbstractPlantServiceTests": "test",
        "service.PlantServiceJdbcTests": "test",  # no test methods of its own; the name says it
        "support.TestPlants": "test_support",
        "rest.dto.PlantDto": "dto",
        "rest.api.PlantsApi": "api_interface",
        "mapper.PlantMapperImpl": "mapper",
    }
    assert {name: g.node(t(name)).attrs["role"] for name in roles} == roles


def test_role_rules_on_edge_cases():
    abtest = TypeDecl(name="ABTest", qualified_name="org.acme.ABTest", kind="class", start_line=1, end_line=2)
    assert type_role(abtest, "main") == "other"  # a main class named *Test is no test
    assert type_role(abtest, "other") == "test"
    helper = TypeDecl(
        name="Fixtures", qualified_name="org.acme.Fixtures", kind="class", start_line=1, end_line=2
    )
    assert type_role(helper, "test") == "test_support"


def test_stub_nodes_for_generated_types(g):
    dto = g.node(t("rest.dto.PlantDto"))
    assert dto.kind == "type" and dto.name == "PlantDto" and dto.path is None
    assert dto.attrs["external"] is True and dto.attrs["generated"] is True
    assert dto.attrs["package"] == f"{G}.rest.dto" and dto.attrs["source_set"] == "main"
    assert g.node(t("rest.api.PlantsApi")).attrs["type_kind"] == "interface"
    stubs = {n.id for n in g.graph.nodes if n.kind == "type" and n.attrs["external"]}
    assert stubs == {
        t("rest.dto.PlantDto"), t("rest.dto.PlantFieldsDto"), t("rest.api.PlantsApi"),
        t("mapper.PlantMapperImpl"),
    }  # fmt: skip


def test_library_types_get_no_nodes(g):
    ids = set(g.nodes)
    for name in ("java.util.List", "org.springframework.http.ResponseEntity", "Clock"):
        assert f"type:{name}" not in ids
    assert not any(i.startswith("type:org.springframework") for i in ids)


def test_project_prefix():
    assert project_prefix(["org.acme.app.web", "org.acme.app.model", ""]) == "org.acme.app"
    assert project_prefix(["org.acme.app"]) == "org.acme.app"
    assert project_prefix(["com.acme", "com.other"]) == ""  # one segment would take in com.*
    assert project_prefix([]) == ""


def test_stubs_only_inside_the_project_prefix():
    files = parse_all({
        "src/main/java/org/acme/app/web/A.java":
            "package org.acme.app.web;\nimport org.acme.app.rest.dto.ADto;\n"
            "import org.other.lib.ThingDto;\nimport org.acme.app.model.B.Inner;\nclass A {}\n",
        "src/main/java/org/acme/app/model/B.java": "package org.acme.app.model;\nclass B {}\n",
    })  # fmt: skip
    index = TypeIndex(files)
    assert index.prefix == "org.acme.app"
    assert index.stubs == {"org.acme.app.rest.dto.ADto": "dto"}  # not a library, not B.Inner


def test_method_node_attrs(g):
    find = g.node(m("service.PlantServiceImpl", "findPlant(int)"))
    assert find.name == "findPlant" and find.path == f"{MAIN}/service/PlantServiceImpl.java"
    assert find.attrs == {
        "signature": "public Plant findPlant(int id)",
        "method_kind": "method",
        "modifiers": ["public"],
        "annotations": ["Override"],
        "return_type": "Plant",
        "parameters": [{"name": "id", "type": "int", "annotations": []}],
        "is_test": False,
        "has_body": True,
        "javadoc": None,
    }
    ctor = g.node(m("service.PlantServiceImpl", "<init>(PlantRepository,SeedStore)"))
    assert ctor.attrs["method_kind"] == "constructor" and ctor.attrs["return_type"] is None
    declared = g.node(m("service.PlantService", "findPlant(int)")).attrs
    assert declared["has_body"] is False and declared["javadoc"] == "Finds one plant."
    fetch = g.node(m("web.SeedController", "fetchSeed(int)")).attrs
    assert fetch["parameters"][0]["annotations"] == ["PathVariable"]
    assert g.node(m("web.PlantControllerTests", "getsPlant()")).attrs["is_test"] is True
    assert g.node(m("web.PlantControllerTests", "setUp()")).attrs["is_test"] is False


def test_endpoint_node_from_the_spec(g):
    node = g.node(ep("GET /plants/{plantId}"))
    assert node.name == "GET /plants/{plantId}" and node.path == SPEC_PATH and node.start_line == 30
    assert node.attrs == {
        "http_method": "GET",
        "spec_path": "/plants/{plantId}",
        "operation_id": "getPlant",
        "summary": "Get a plant",
        "description": "One plant, by id.",
        "tags": ["plants"],
        "parameters": [{"name": "plantId", "location": "path", "required": True, "schema": "integer"}],
        "request_body": None,
        "responses": {"200": "Plant", "304": "Not modified.", "404": "ProblemDetail"},
        "url_path": "/garden/api/plants/{plantId}",
        "served_path": "/api/plants/{plantId}",
        "sources": ["openapi"],
    }


def test_schema_and_doc_nodes(g):
    plant = g.node(f"schema:{SPEC_PATH}#Plant")
    assert plant.name == "Plant" and plant.path == SPEC_PATH and plant.start_line == 110
    assert plant.attrs == {
        "properties": {"id": "integer", "seeds": "array<Seed>"},
        "required": ["id"],
        "all_of": ["PlantFields"],
    }
    section = g.node("doc:README.md#client")
    assert section.name == "Client" and (section.start_line, section.end_line) == (9, 12)
    assert section.attrs == {"level": 2, "breadcrumbs": ["Garden"]}
    assert g.node("doc:README.md").name == "README.md"  # the text before the first heading


# ---- type resolution -----------------------------------------------------------------------


def test_resolve_follows_java_scoping():
    files = parse_all({
        "src/main/java/org/acme/app/model/Owner.java":
            "package org.acme.app.model;\npublic class Owner { public static class Address {} }\n",
        "src/main/java/org/acme/app/model/Pet.java": "package org.acme.app.model;\npublic class Pet {}\n",
        "src/main/java/org/acme/app/web/Pet.java": "package org.acme.app.web;\npublic class Pet {}\n",
        "src/main/java/org/acme/app/web/Repository.java":
            "package org.acme.app.web;\npublic class Repository {}\n",
        "src/main/java/org/acme/app/web/Page.java":
            "package org.acme.app.web;\nimport org.acme.app.model.Owner;\nimport org.acme.app.model.*;\n"
            "import org.springframework.data.repository.Repository;\n"
            "public class Page { class Inner { class Deeper {} } }\n",
        "src/main/java/org/acme/app/service/Shop.java":
            "package org.acme.app.service;\nimport org.acme.app.model.*;\nclass Shop {}\n",
    })  # fmt: skip
    index = TypeIndex(files)
    page, shop = files[4], files[5]
    inner = "org.acme.app.web.Page.Inner"
    assert index.resolve("org.acme.app.model.Pet", page) == "org.acme.app.model.Pet"  # qualified
    assert index.resolve("Inner", page, "org.acme.app.web.Page") == inner  # nested
    assert index.resolve("Page", page, f"{inner}.Deeper") == "org.acme.app.web.Page"  # outer
    assert index.resolve("Deeper", page, f"{inner}.Deeper") == f"{inner}.Deeper"
    assert index.resolve("Page.Inner", page) == inner
    assert index.resolve("Owner", page) == "org.acme.app.model.Owner"  # single-type import
    assert index.resolve("Owner.Address", page) == "org.acme.app.model.Owner.Address"
    assert index.resolve("Pet", page) == "org.acme.app.web.Pet"  # same package beats a wildcard
    assert index.resolve("Pet", shop) == "org.acme.app.model.Pet"  # wildcard import
    assert index.resolve("Repository", page) is None  # a library import hides the project type
    for name in ("int", "String", "Object", "Unknown", "?", "java.util.List"):
        assert index.resolve(name, page) is None


# ---- CONTAINS, EXTENDS, IMPLEMENTS -----------------------------------------------------------


def test_contains(g):
    assert g.has("CONTAINS", f"file:{MAIN}/model/Plant.java", t("model.Plant"))
    assert not g.has("CONTAINS", f"file:{MAIN}/model/Plant.java", t("model.Plant.Tag"))
    assert g.has("CONTAINS", t("model.Plant"), t("model.Plant.Tag"))
    assert g.has("CONTAINS", t("model.Plant"), m("model.Plant", "<init>(String)"))
    assert g.has("CONTAINS", t("model.Plant.Tag"), m("model.Plant.Tag", "attach(Plant)"))
    assert g.has("CONTAINS", f"file:{SPEC_PATH}", ep("GET /plants"))
    assert g.has("CONTAINS", f"file:{SPEC_PATH}", f"schema:{SPEC_PATH}#Seed")
    assert g.has("CONTAINS", "file:README.md", "doc:README.md#client")
    assert g.has("CONTAINS", "file:README.md", "doc:README.md")


def test_extends_and_implements(g):
    assert g.has("EXTENDS", t("model.Plant"), t("model.BaseEntity"))
    assert g.has("EXTENDS", t("service.PlantServiceImpl"), t("service.AuditedService"))
    assert g.has("IMPLEMENTS", t("service.PlantServiceImpl"), t("service.PlantService"))
    assert g.has("EXTENDS", t("service.PlantService"), t("service.Finder"))  # interface extends
    assert g.has("IMPLEMENTS", t("web.PlantController"), t("rest.api.PlantsApi"))  # a stub
    assert g.has("EXTENDS", t("service.PlantServiceJdbcTests"), t("service.AbstractPlantServiceTests"))
    assert g.out("EXTENDS", t("repository.PlantRepository")) == set()  # JpaRepository: library


# ---- INJECTS -------------------------------------------------------------------------------


def test_injects_by_constructor_field_and_setter(g):
    impl = t("service.PlantServiceImpl")
    assert g.edge("INJECTS", impl, t("repository.PlantRepository")).attrs == {
        "via": "constructor", "name": "plantRepository",
    }  # fmt: skip
    assert g.edge("INJECTS", impl, t("repository.SeedStore")).attrs["via"] == "constructor"
    assert g.edge("INJECTS", impl, t("util.GardenUtils")).attrs == {"via": "field", "name": "utils"}
    assert g.edge("INJECTS", impl, t("service.AuditLog")).attrs == {"via": "setter", "name": "auditLog"}
    assert g.out("INJECTS", impl) == {
        t("repository.PlantRepository"), t("repository.SeedStore"), t("util.GardenUtils"),
        t("service.AuditLog"),
    }  # Clock is a library type  # fmt: skip
    assert g.out("INJECTS", t("web.PlantController")) == {
        t("service.PlantService"), t("mapper.PlantMapper"),
    }  # fmt: skip
    assert g.out("INJECTS", t("web.SeedController")) == {t("repository.SeedStore")}


def test_entities_and_containers_in_injection():
    files = parse_all({
        "src/main/java/org/acme/app/Plant.java":
            "package org.acme.app;\n@Entity\npublic class Plant { Plant(Pot pot) {} }\n",
        "src/main/java/org/acme/app/Pot.java": "package org.acme.app;\npublic class Pot {}\n",
        "src/main/java/org/acme/app/Rules.java":
            "package org.acme.app;\nimport java.util.List;\nimport java.util.Map;\n"
            "@Service\npublic class Rules {\n"
            "    Rules(List<Pot> pots, Map<String, Plant> plants) {}\n}\n",
    })  # fmt: skip
    g = View(build_graph(files))
    assert g.out("INJECTS", "type:org.acme.app.Plant") == set()  # an entity's arguments are data
    assert g.out("INJECTS", "type:org.acme.app.Rules") == {"type:org.acme.app.Pot", "type:org.acme.app.Plant"}


# ---- CALLS, USES ---------------------------------------------------------------------------


def calls(g: View, source: str) -> dict[str, str]:
    """target -> resolution of the CALLS edges from a method."""
    return {e.target: e.attrs["resolution"] for e in g.graph.edges if e.kind == "CALLS" and e.source == source}


def test_calls_by_receiver_type_with_overloads_by_arity(g):
    found = calls(g, m("service.PlantServiceImpl", "findAll()"))
    assert found[m("repository.PlantRepository", "findByName(String)")] == "receiver_type"
    assert found[m("repository.PlantRepository", "findByName(String,int)")] == "receiver_type"
    assert found[m("service.PlantServiceImpl", "water(Plant)")] == "same_type"  # this::water


def test_calls_same_type_super_static_new_and_name_only(g):
    found = calls(g, m("service.PlantServiceImpl", "savePlant(Plant)"))
    assert found == {
        m("util.GardenUtils", "normalise(String)"): "receiver_type",  # static call
        m("model.Plant", "getName()"): "receiver_type",
        m("model.Plant", "<init>(String)"): "receiver_type",  # new Plant(clean), not Plant()
        m("service.PlantServiceImpl", "findPlant(int)"): "same_type",
        m("model.Plant", "describe()"): "name_only",  # findPlant(1).describe(): no receiver type
        m("service.PlantServiceImpl", "water(Plant)"): "same_type",  # this.water(copy)
        m("service.AuditLog", "record(String)"): "receiver_type",
    }
    # unqualified audit() is the class's own override; super.audit() goes to the superclass
    assert calls(g, m("service.PlantServiceImpl", "findPlant(int)")) == {
        m("service.PlantServiceImpl", "audit(String)"): "same_type",
    }  # findById: inherited from JpaRepository, a library  # fmt: skip
    assert calls(g, m("service.PlantServiceImpl", "audit(String)")) == {
        m("service.AuditedService", "audit(String)"): "same_type",
    }  # fmt: skip
    ctor = m("service.PlantServiceImpl", "<init>(PlantRepository,SeedStore)")
    assert calls(g, ctor) == {m("service.AuditedService", "<init>(String)"): "same_type"}


def test_calls_through_supertypes_and_outer_types(g):
    found = calls(g, m("model.Plant.Tag", "attach(Plant)"))
    assert found == {
        m("model.Plant", "describe()"): "receiver_type",
        m("model.Plant", "getName()"): "same_type",  # a method of the outer type
    }
    add_tag = calls(g, m("model.Plant", "addTag(String)"))
    assert add_tag[m("model.Plant.Tag", "<init>(String)")] == "receiver_type"
    assert add_tag[m("model.Plant.Tag", "attach(Plant)")] == "receiver_type"
    # findOne() calls findPlant(Integer) by arity; findAll() of the interface by receiver type
    find_one = calls(g, m("service.PlantServiceImpl", "findOne(Integer)"))
    assert m("service.PlantServiceImpl", "findPlant(int)") in find_one
    assert m("service.PlantService", "findAll()") in calls(g, m("web.PlantController", "listPlants()"))


def test_same_arity_overloads_are_all_linked(g):
    found = calls(g, m("service.PlantServiceImpl", "store(Plant)"))
    assert set(found) == {
        m("service.PlantServiceImpl", "savePlant(Plant)"),
        m("service.PlantServiceImpl", "savePlant(String)"),
    }


def test_calls_record_the_first_line_once(g):
    source = m("service.PlantServiceImpl", "savePlant(Plant)")
    edge = g.edge("CALLS", source, m("service.AuditLog", "record(String)"))
    line = line_of(f"{MAIN}/service/PlantServiceImpl.java", "auditLog.record(clean);")
    assert edge.attrs == {"line": line, "resolution": "receiver_type"}  # called twice, one edge


def test_library_calls_get_no_edges(g):
    targets = calls(g, m("web.PlantController", "listPlants()"))
    assert all(target.startswith(f"method:{G}.") for target in targets)
    assert calls(g, m("util.GardenUtils", "normalise(String)")) == {}  # name.trim()


def test_name_only_needs_one_candidate():
    files = parse_all({
        "src/main/java/org/acme/app/Box.java":
            "package org.acme.app;\npublic class Box { String label() { return \"\"; } }\n",
        "src/main/java/org/acme/app/Crate.java":
            "package org.acme.app;\npublic class Crate { String label() { return \"\"; } }\n",
        "src/main/java/org/acme/app/Named.java":
            "package org.acme.app;\npublic interface Named { String title(); }\n",
        "src/main/java/org/acme/app/Card.java":
            "package org.acme.app;\n"
            "public class Card implements Named { public String title() { return \"\"; } }\n",
        "src/main/java/org/acme/app/Shelf.java":
            "package org.acme.app;\npublic class Shelf {\n"
            "    void show(Shelf s) { s.next().label(); s.next().title(); s.next().size(); }\n"
            "    Shelf next() { return this; }\n    int size() { return 0; }\n}\n",
    })  # fmt: skip
    g = View(build_graph(files))
    found = calls(g, "method:org.acme.app.Shelf#show(Shelf)")
    assert "method:org.acme.app.Box#label()" not in found  # Box or Crate: ambiguous
    assert found["method:org.acme.app.Named#title()"] == "name_only"  # an override counts once
    assert "method:org.acme.app.Shelf#size()" not in found  # too common a JDK name


def test_uses_parameter_return_generics_and_new(g):
    def uses(source: str) -> dict[str, str]:
        return {e.target: e.attrs["as"] for e in g.graph.edges if e.kind == "USES" and e.source == source}

    assert uses(m("mapper.PlantMapper", "toPlantDto(Plant)")) == {
        t("model.Plant"): "parameter", t("rest.dto.PlantDto"): "return",
    }  # fmt: skip
    assert uses(m("mapper.PlantMapper", "toPlantDtos(List)")) == {
        t("model.Plant"): "parameter", t("rest.dto.PlantDto"): "return",
    }  # generic arguments  # fmt: skip
    assert uses(m("mapper.PlantMapper", "toPlant(PlantFieldsDto)")) == {
        t("rest.dto.PlantFieldsDto"): "parameter", t("model.Plant"): "return",
    }  # fmt: skip
    assert uses(m("service.PlantServiceImpl", "savePlant(String)")) == {t("model.Plant"): "new"}
    assert uses(m("service.PlantServiceImpl", "savePlant(Plant)")) == {t("model.Plant"): "parameter"}
    assert uses(m("web.PlantController", "getPlant(Integer)")) == {t("rest.dto.PlantDto"): "return"}


# ---- OVERRIDES -----------------------------------------------------------------------------


def test_overrides_transitively_with_type_variables(g):
    impl = "service.PlantServiceImpl"
    assert g.out("OVERRIDES", m(impl, "findPlant(int)")) == {m("service.PlantService", "findPlant(int)")}
    assert g.out("OVERRIDES", m(impl, "findOne(Integer)")) == {m("service.Finder", "findOne(Integer)")}
    assert g.out("OVERRIDES", m(impl, "store(Plant)")) == {m("service.Finder", "store(T)")}  # T
    assert g.out("OVERRIDES", m(impl, "savePlant(Plant)")) == {m("service.PlantService", "savePlant(Plant)")}
    assert g.out("OVERRIDES", m(impl, "savePlant(String)")) == set()  # other parameter type
    assert g.out("OVERRIDES", m(impl, "audit(String)")) == {m("service.AuditedService", "audit(String)")}
    assert g.out("OVERRIDES", m(impl, "water(Plant)")) == set()
    assert g.out("OVERRIDES", m(impl, "<init>(PlantRepository,SeedStore)")) == set()


# ---- SERVES --------------------------------------------------------------------------------


def test_serves_by_operation_id_with_the_class_prefix(g):
    get_plant = g.edge("SERVES", m("web.PlantController", "getPlant(Integer)"), ep("GET /plants/{plantId}"))
    assert get_plant.attrs == {"via": "operation_id"}
    assert g.has("SERVES", m("web.PlantController", "listPlants()"), ep("GET /plants"))
    tag = ep("PUT /plants/{plantId}/tags/{tag}")
    assert g.has("SERVES", m("web.PlantController", "tagPlant(Integer,String)"), tag)
    assert g.node(ep("GET /plants")).attrs["served_path"] == "/api/plants"  # "api", no slash
    assert g.node(tag).attrs["served_path"] == "/api/plants/{plantId}/tags/{tag}"
    # getSeed is an operation of tag "seeds"; PlantController implements PlantsApi only
    assert g.out("SERVES", m("web.PlantController", "getSeed()")) == set()
    assert g.into("SERVES", ep("POST /plants")) == set()
    assert g.into("SERVES", ep("GET /oops")) == set()


def test_serves_by_annotation_merges_with_the_spec(g):
    seed = g.node(ep("GET /seeds/{seedId}"))
    assert g.edge("SERVES", m("web.SeedController", "fetchSeed(int)"), seed.id).attrs == {"via": "annotation"}
    assert seed.attrs["sources"] == ["openapi", "code"]
    assert seed.attrs["served_path"] == "/api/seeds/{seedId}"


def test_serves_by_annotation_makes_code_only_endpoints(g):
    delete = g.node(ep("DELETE /api/seeds/{id}"))
    assert delete.name == "DELETE /api/seeds/{id}"
    path = f"{MAIN}/web/SeedController.java"
    assert delete.path == path and delete.start_line == line_of(path, "@DeleteMapping")
    assert delete.attrs["sources"] == ["code"] and delete.attrs["spec_path"] is None
    assert delete.attrs["served_path"] == "/api/seeds/{id}" and delete.attrs["url_path"] is None
    assert g.has("SERVES", m("web.SeedController", "deleteSeed(int)"), delete.id)
    # a literal segment is another route than the spec's {seedId}
    assert g.has("SERVES", m("web.SeedController", "stats()"), ep("GET /api/seeds/stats"))
    assert g.has("SERVES", m("web.SeedController", "ping()"), ep("ANY /api/ping"))  # no method


def test_serves_with_placeholders_and_untagged_operations():
    files = parse_all({
        "src/main/java/org/acme/app/model/Bell.java": "package org.acme.app.model;\nclass Bell {}\n",
        "src/main/java/org/acme/app/web/PingController.java":
            "package org.acme.app.web;\nimport org.acme.app.rest.api.DefaultApi;\n"
            '@RequestMapping("${api.base:/v1}")\n'
            "public class PingController implements DefaultApi {\n    public void ping() {}\n}\n",
        "src/main/java/org/acme/app/web/HiddenController.java":
            'package org.acme.app.web;\n@RestController\n@RequestMapping("${api.base}")\n'
            "public class HiddenController {\n    public void hide() {}\n"
            '    @GetMapping("/hidden/x")\n    public void x() {}\n}\n',
    })  # fmt: skip
    operations = [
        ApiOperation("GET", "/ping", operation_id="ping"),  # no tag: generated into DefaultApi
        ApiOperation("GET", "/hide", operation_id="hide", tags=["hidden"]),
    ]
    g = View(build_graph(files, [ApiSpec("api.yml", operations=operations)]))
    assert g.has("SERVES", "method:org.acme.app.web.PingController#ping()", "endpoint:GET /ping")
    assert g.node("endpoint:GET /ping").attrs["served_path"] == "/v1/ping"  # the default
    assert g.has("SERVES", "method:org.acme.app.web.HiddenController#hide()", "endpoint:GET /hide")
    assert g.node("endpoint:GET /hide").attrs["served_path"] is None  # ${api.base}: unknown
    assert g.out("SERVES", "method:org.acme.app.web.HiddenController#x()") == set()


# ---- TESTS, MOCKS --------------------------------------------------------------------------


def test_nested_test_classes_go_by_their_outer_class():
    files = parse_all({
        "src/main/java/org/acme/app/Bell.java": "package org.acme.app;\npublic class Bell {}\n",
        "src/test/java/org/acme/app/BellTests.java":
            "package org.acme.app;\nclass BellTests {\n    @Nested\n    class WhenRung {\n"
            "        @Test\n        void rings() {}\n    }\n}\n",
    })  # fmt: skip
    g = View(build_graph(files))
    assert g.node("type:org.acme.app.BellTests.WhenRung").attrs["role"] == "test"
    assert reasons_from(g, "type:org.acme.app.BellTests.WhenRung") == {"type:org.acme.app.Bell": "name"}
    assert reasons_from(g, "type:org.acme.app.BellTests") == {"type:org.acme.app.Bell": "name"}


def reasons_from(g: View, source: str) -> dict[str, str]:
    return {e.target: e.attrs["reason"] for e in g.graph.edges if e.kind == "TESTS" and e.source == source}


def test_tests_by_name_including_abstract_bases_and_other_packages(g):
    assert reasons_from(g, t("web.PlantControllerTests")) == {t("web.PlantController"): "name"}
    assert reasons_from(g, t("service.AbstractPlantServiceTests")) == {t("service.PlantService"): "name"}
    assert reasons_from(g, t("service.PlantServiceImplTests")) == {t("service.PlantServiceImpl"): "name"}
    assert reasons_from(g, "type:org.acme.checks.GardenUtilsTest") == {t("util.GardenUtils"): "name"}
    assert reasons_from(g, t("service.PlantServiceJdbcTests")) == {}  # it EXTENDS the base


def test_tests_by_fields_and_annotation(g):
    assert reasons_from(g, t("GardenFlowTests")) == {
        t("service.PlantService"): "field",  # @Autowired
        t("repository.SeedStore"): "field",  # = new SeedStore()
    }
    assert reasons_from(g, t("web.SeedControllerWebTests")) == {t("web.SeedController"): "annotation"}


def test_mocks(g):
    def via(test: str, mocked: str) -> str:
        return g.edge("MOCKS", t(test), t(mocked)).attrs["via"]

    assert via("web.PlantControllerTests", "service.PlantService") == "MockitoBean"
    assert via("web.SeedControllerWebTests", "repository.SeedStore") == "MockBean"
    assert via("web.SeedControllerWebTests", "util.GardenUtils") == "mock()"
    assert via("service.PlantServiceImplTests", "repository.PlantRepository") == "Mock"
    assert g.out("MOCKS", t("web.PlantControllerTests")) == {t("service.PlantService")}


def test_test_methods_test_what_they_call_but_not_mocks(g):
    tests = "web.PlantControllerTests"
    assert reasons_from(g, m(tests, "mapsPlant()")) == {
        m("mapper.PlantMapper", "toPlantDto(Plant)"): "call",
        t("model.Plant"): "new",
    }
    gets = reasons_from(g, m(tests, "getsPlant()"))
    assert m("service.PlantService", "findPlant(int)") not in gets  # given(mocked.findPlant(1))
    assert gets[t("model.Plant")] == "new"
    assert reasons_from(g, m("service.AbstractPlantServiceTests", "findsPlant()")) == {
        m("service.PlantService", "findPlant(int)"): "call",
        m("model.Plant", "describe()"): "call",
    }
    assert reasons_from(g, m("service.PlantServiceImplTests", "savesPlant()")) == {
        m("service.PlantServiceImpl", "savePlant(String)"): "call",
        m("service.PlantServiceImpl", "savePlant(Plant)"): "call",  # same arity: both
    }  # not PlantRepository#findByName: mocked
    trims = "method:org.acme.checks.GardenUtilsTest#trims()"
    assert reasons_from(g, trims) == {m("util.GardenUtils", "normalise(String)"): "call"}
    assert reasons_from(g, m(tests, "setUp()")) == {}  # not a test method


def test_test_methods_test_endpoints_by_mockmvc_urls(g):
    tests = "web.PlantControllerTests"
    gets = g.edge("TESTS", m(tests, "getsPlant()"), ep("GET /plants/{plantId}"))
    line = line_of(f"{TEST}/web/PlantControllerTests.java", "/api/plants/1?verbose=true")
    assert gets.attrs == {"reason": "http", "line": line}  # query string ignored
    assert reasons_from(g, m(tests, "tagsPlant()")) == {ep("PUT /plants/{plantId}/tags/{tag}"): "http"}
    assert reasons_from(g, m(tests, "listsPlantsAndSeeds()")) == {
        ep("GET /plants"): "http",  # GET only, not POST /plants
        ep("GET /api/seeds/stats"): "http",  # request(...): any method; most literal segments
        ep("DELETE /api/seeds/{id}"): "http",
    }
    assert reasons_from(g, m(tests, "callsTheClientUrl()")) == {ep("GET /plants/{plantId}"): "http"}
    assert reasons_from(g, m(tests, "callsAnotherPrefix()")) == {ep("GET /plants/{plantId}"): "http"}


def test_match_url():
    def endpoint(method: str, spec: str | None, served: str | None, url: str | None) -> Node:
        attrs = {"http_method": method, "spec_path": spec, "served_path": served, "url_path": url}
        return Node(f"endpoint:{method} {spec or served}", "endpoint", "", attrs=attrs)

    endpoints = [
        endpoint("GET", "/owners/{ownerId}", "/api/owners/{ownerId}", "/shop/api/owners/{ownerId}"),
        endpoint("GET", "/owners/{ownerId}/pets/{petId}", "/api/owners/{ownerId}/pets/{petId}", None),
        endpoint("PUT", "/owners/{ownerId}/pets/{petId}", "/api/owners/{ownerId}/pets/{petId}", None),
        endpoint("GET", "/owners/search", "/api/owners/search", None),
        endpoint("GET", "/v2/owners", "/api/v2/owners", None),
    ]
    assert match_url("/api/owners/1", "GET", endpoints) == ["endpoint:GET /owners/{ownerId}"]
    assert match_url("/api/owners/search?q=x", "GET", endpoints) == ["endpoint:GET /owners/search"]
    assert match_url("/api/v2/owners?page=0&size=2", "GET", endpoints) == ["endpoint:GET /v2/owners"]
    pets = "endpoint:PUT /owners/{ownerId}/pets/{petId}"
    assert match_url("/api/owners/{}/pets/{}", "PUT", endpoints) == [pets]
    assert len(match_url("/api/owners/{}/pets/{}", None, endpoints)) == 2  # any method: a tie
    assert match_url("/shop/api/owners/1", "GET", endpoints) == ["endpoint:GET /owners/{ownerId}"]
    assert match_url("/api/owners/1", "DELETE", endpoints) == []
    assert match_url("/{}", "GET", endpoints) == []  # nothing literal to go by
    assert match_url("owners", "GET", endpoints) == []  # not a path


# ---- GENERATED_FROM, REFERENCES, MENTIONS --------------------------------------------------


def test_generated_from(g):
    assert g.out("GENERATED_FROM", t("rest.dto.PlantDto")) == {f"schema:{SPEC_PATH}#Plant"}
    assert g.out("GENERATED_FROM", t("rest.dto.PlantFieldsDto")) == {f"schema:{SPEC_PATH}#PlantFields"}
    assert g.out("GENERATED_FROM", t("rest.api.PlantsApi")) == set()


def test_references(g):
    def refs(source: str) -> dict[str, dict]:
        return {e.target: e.attrs for e in g.graph.edges if e.kind == "REFERENCES" and e.source == source}

    schema = f"schema:{SPEC_PATH}#"
    assert refs(ep("GET /plants")) == {schema + "Plant": {"as": "response", "status": "200"}}  # array<Plant>
    assert refs(ep("POST /plants")) == {
        schema + "PlantFields": {"as": "request"},
        schema + "Plant": {"as": "response", "status": "201"},
        schema + "ProblemDetail": {"as": "response", "status": "400"},
    }
    assert set(refs(ep("GET /plants/{plantId}"))) == {schema + "Plant", schema + "ProblemDetail"}
    assert refs(ep("GET /oops")) == {}  # "string"
    assert refs(schema + "Plant") == {
        schema + "PlantFields": {"as": "all_of"},
        schema + "Seed": {"as": "property", "property": "seeds"},
    }


def test_mentions(g):
    assert g.out("MENTIONS", "doc:README.md#garden") == {
        t("service.PlantService"), t("web.PlantController"), t("util.GardenUtils"),
        t("model.BaseEntity"), t("service.PlantServiceImpl"),
        ep("GET /plants/{plantId}"),  # `/api/plants/{id}`: a served_path, other param name
    }  # not Plant (too plain), not PlantServiceImplX  # fmt: skip
    assert g.out("MENTIONS", "doc:README.md#client") == {
        ep("GET /seeds/{seedId}"),  # its url_path
        ep("POST /plants"),  # the spec path; the line says POST, so not GET /plants
    }
    assert g.out("MENTIONS", "doc:README.md#empty") == set()


def test_mentions_are_capped_per_section():
    sources = {
        f"src/main/java/org/acme/app/Widget{i:02}Box.java":
            f"package org.acme.app;\nclass Widget{i:02}Box {{}}\n"
        for i in range(35)
    }
    sources["src/main/java/org/acme/app/model/Base.java"] = "package org.acme.app.model;\nclass Base {}\n"
    text = "# All\n" + " ".join(f"Widget{i:02}Box" for i in reversed(range(35))) + "\n"
    graph = build_graph(parse_all(sources), docs=[DocSection("docs/all.md", "All", "all", 1, 1, 2, text)])
    mentioned = View(graph).out("MENTIONS", "doc:docs/all.md#all")
    assert len(mentioned) == 30
    assert mentioned == {f"type:org.acme.app.Widget{i:02}Box" for i in range(5, 35)}  # first in text


# ---- the graph as a whole ------------------------------------------------------------------


def test_sorted_without_duplicates_or_dangling_edges(g):
    ids = [n.id for n in g.graph.nodes]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    keys = [(e.kind, e.source, e.target) for e in g.graph.edges]
    assert keys == sorted(keys) and len(keys) == len(set(keys))
    assert all(e.source in g.nodes and e.target in g.nodes for e in g.graph.edges)


def test_every_edge_kind_is_built(g):
    kinds = {e.kind for e in g.graph.edges}
    assert kinds == {
        "CONTAINS", "EXTENDS", "IMPLEMENTS", "INJECTS", "CALLS", "OVERRIDES", "USES", "SERVES",
        "TESTS", "MOCKS", "GENERATED_FROM", "REFERENCES", "MENTIONS",
    }  # fmt: skip


def test_deterministic_whatever_the_input_order(java_files, g):
    shuffled = list(java_files)
    random.Random(7).shuffle(shuffled)
    again = build_graph(shuffled, [SPEC], list(reversed(DOCS)))
    assert to_dict(again) == to_dict(g.graph)


def test_pure_and_empty_inputs():
    assert build_graph([]) == Graph()
    files = parse_all(JAVA)
    before = to_dict(files)
    build_graph(files, [SPEC], DOCS)
    assert to_dict(files) == before  # the inputs are not changed
