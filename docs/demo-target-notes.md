# Demo target: spring-petclinic-rest

Facts about the demo target that shaped the design, from a survey of commit `afc8fc1`
(Spring Boot 4.1.1, openapi-generator 7.25.0, MapStruct 1.6.3, Jackson 3). Paths are under
`src/main/java/org/springframework/samples/petclinic/` unless shown in full.

## Layout

87 main and 22 test Java files. Packages: `config`, `mapper` (7), `model` (12), `util`,
`service` (ClinicService/Impl, UserService/Impl), `security`, `repository` (7 interfaces) with
`jdbc/`, `jpa/` and `springdatajpa/` implementations, `rest/advice`, `rest/controller/v1` (8),
`rest/controller/v2` (2), `rest/validation`. 4 files are `package-info.java`.

## Endpoints are API-first

- Controllers have **no mapping annotations on their methods**. They implement interfaces
  generated at build time from `src/main/resources/openapi.yml` and carry only `@Override`,
  `@PreAuthorize`, sometimes `@Transactional`.
- Generator config (pom.xml): `apiPackage=...rest.api`, `modelPackage=...rest.dto`,
  `modelNameSuffix=Dto`, `interfaceOnly=true`, `useTags=true`. Interface name = camelised tag +
  `Api`: tag `owners` -> `OwnersApi`, `owner-v2` -> `OwnerV2Api`.
- **Every controller method name equals an operationId** (36 of 37; `failingRequest` GET /oops has
  no implementation). Example: `OwnerRestControllerV1 implements OwnersApi`, `getOwner(Integer)`
  -> GET /owners/{ownerId}.
- The only class-level mapping is `@RequestMapping("api")` (no leading slash) on 5 controllers.
  `RootRestControllerV1` has `@RequestMapping("/")` and redirects to swagger-ui (not in the spec).
- Paths: spec `servers: http://localhost:9966/petclinic/api`; `server.servlet.context-path=/petclinic/`.
  At runtime: `/petclinic` + `/api` + spec path. MockMvc tests call `/api/...`.

## Types that are not in the source tree (generated at build time)

- `...rest.api.*Api` (10 interfaces), `...rest.dto.*Dto` (one per schema, 17; fluent setters in
  tests: `new OwnerDto().id(1).firstName(..)`), MapStruct `*MapperImpl`.
- `SpringData*Repository` interfaces have no class at all (Spring Data proxies).

## Dependency injection

Mixed: constructor injection without `@Autowired` (controllers, `ClinicServiceImpl` with 6
repositories), `@Autowired` fields (`UserServiceImpl`, `BasicAuthenticationConfig`),
`@PersistenceContext EntityManager` fields.

## Repositories

Each of the 7 repository interfaces has 3 implementations chosen by Spring profile:
`Jdbc*RepositoryImpl` (`@Profile("jdbc")`), `Jpa*RepositoryImpl` (`@Profile("jpa")`) and a Spring
Data interface (`@Profile("spring-data-jpa")`). Defaults: `h2,spring-data-jpa` (main),
`hsqldb,spring-data-jpa` (tests). Overloads exist (`findByLastName(String)` and
`findByLastName(String, Pageable)`). Repository calls often sit in lambdas:
`findEntityById(() -> ownerRepository.findById(id))`.

## Tests

- JUnit 5 only; no `@WebMvcTest`, `@MockBean` or `@InjectMocks`.
- Controller tests: `@SpringBootTest @ContextConfiguration(classes = ApplicationTestConfig.class)
  @WebAppConfiguration`, an `@Autowired` controller field, `@MockitoBean ClinicService`,
  `MockMvcBuilders.standaloneSetup(controller).setControllerAdvice(...)`, `@WithMockUser`,
  BDD `given(..).willReturn`, `jsonPath`. `get`/`post` come from a static wildcard import.
- URLs: literals `get("/api/owners/1")`, query strings `get("/api/v2/owners?page=0&size=2")`,
  concatenations `put("/api/owners/" + ownerId + "/pets/" + petId)`.
- Naming: `*Tests` (21 files); `PetAgeValidatorTest` lives in another package than its subject;
  `V2RestControllersTests` covers two controllers.
- Abstract bases: `AbstractClinicServiceTests` (`@Autowired ClinicService`, 37 tests) with 4
  subclasses that only change `@ActiveProfiles` (jdbc on H2, jdbc on HSQLDB, jpa,
  spring-data-jpa); same for `AbstractUserServiceTests`. They test the interface type.

## Java features present

Generics (diamonds, generic methods, wildcards `Class<? extends Payload>[]`), lambdas, method
references, `var` (tests), static nested classes, anonymous classes, default interface methods,
`@interface`, annotation arrays and nested annotations, repeated annotations, multi-catch, static
and wildcard imports, mixed tabs and spaces. No records, enums, Lombok or text blocks.

## OpenAPI and docs

- `openapi.yml`: OpenAPI 3.0.1, 19 paths, 37 operations, every one with an operationId and one tag
  (10 tags); 17 schemas, 5 with `allOf` (e.g. `Owner = allOf[OwnerFields, {id, pets}]`); all
  `$ref`s point to `#/components/schemas/X`; path parameters inline.
- Docs: `readme.md` (setup, API endpoint table with `/api/...` paths, database profiles, API-first
  approach, generated code, security), plus `src/test/postman/README.md` and
  `src/test/jmeter/README.md`.
