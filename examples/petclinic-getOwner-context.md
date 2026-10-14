# Test context: OwnerRestControllerV1#getOwner(Integer)

method `method:org.springframework.samples.petclinic.rest.controller.v1.OwnerRestControllerV1#getOwner(Integer)` · controller · src/main/java/org/springframework/samples/petclinic/rest/controller/v1/OwnerRestControllerV1.java:89-97

## Target

`public ResponseEntity<OwnerDto> getOwner(Integer ownerId)`
```java
@PreAuthorize("hasRole(@roles.OWNER_ADMIN)")
@Override
public ResponseEntity<OwnerDto> getOwner(Integer ownerId) {
    Owner owner = this.clinicService.findOwnerById(ownerId);
    if (owner == null) {
        return new ResponseEntity<>(HttpStatus.NOT_FOUND);
    }
    return new ResponseEntity<>(ownerMapper.toOwnerDto(owner), HttpStatus.OK);
}
```

## Enclosing class: OwnerRestControllerV1 (controller)

@RestController @CrossOrigin @RequestMapping · src/main/java/org/springframework/samples/petclinic/rest/controller/v1/OwnerRestControllerV1.java:51-203
```java
/**
 * @author Vitaliy Fedoriv
 */
@RestController
@CrossOrigin(exposedHeaders = "errors, content-type")
@RequestMapping("/api")
public class OwnerRestControllerV1 implements OwnersApi {
    private final ClinicService clinicService;
    private final OwnerMapper ownerMapper;
    private final PetMapper petMapper;
    private final VisitMapper visitMapper;

    public OwnerRestControllerV1(ClinicService clinicService, OwnerMapper ownerMapper, PetMapper petMapper, VisitMapper visitMapper);
    // ... 8 more methods: get_node or `tcg show type:org.springframework.samples.petclinic.rest.controller.v1.OwnerRestControllerV1`
    @PreAuthorize("hasRole(@roles.OWNER_ADMIN)") @Override public ResponseEntity<OwnerDto> getOwner(Integer ownerId);
}
```

## Dependencies to mock

- **ClinicService** (interface; field `clinicService` via constructor)
  - calls `Owner findOwnerById(int id) throws DataAccessException`
  - in tests: mocked in 7 test classes, e.g. OwnerRestControllerV1Tests (@MockitoBean), PetRestControllerV1Tests (@MockitoBean); injected for real in 1 test class, e.g. AbstractClinicServiceTests
- **OwnerMapper** (interface, mapper; field `ownerMapper` via constructor)
  - calls `OwnerDto toOwnerDto(Owner owner)`
  - in tests: injected for real in 2 test classes, e.g. OwnerRestControllerV1Tests, V2RestControllersTests
- **PetMapper** (interface, mapper; field `petMapper` via constructor) — not called by the target; needed to build the class
  - in tests: injected for real in 3 test classes, e.g. OwnerRestControllerV1Tests, PetRestControllerV1Tests
- **VisitMapper** (interface, mapper; field `visitMapper` via constructor) — not called by the target; needed to build the class
  - in tests: injected for real in 2 test classes, e.g. OwnerRestControllerV1Tests, VisitRestControllerV1Tests

## Data types

- **OwnerDto** — generated from OpenAPI schema `Owner` (src/main/resources/openapi.yml); return of getOwner; return of OwnerMapper#toOwnerDto(Owner)
  - properties (* required): firstName: string*, lastName: string*, address: string*, city: string*, telephone: string*, id: integer, pets: array<Pet>*
- **Owner** (entity class); return of ClinicService#findOwnerById(int); parameter of OwnerMapper#toOwnerDto(Owner)
  - fields: address: String @Column @NotEmpty, city: String @Column @NotEmpty, telephone: String @Column @NotEmpty @Digits @Pattern, pets: Set<Pet> @OneToMany, firstName: String @Column @NotEmpty (from Person), lastName: String @Column @NotEmpty (from Person), id: Integer @Id @GeneratedValue (from BaseEntity)

## Endpoints

- **GET /owners/{ownerId}** — operationId `getOwner`: Get a pet owner by ID
  - served at `/api/owners/{ownerId}` (what MockMvc calls); clients call `/petclinic/api/owners/{ownerId}`
  - parameters: ownerId (path, required): integer
  - responses: 200 Owner · 304 Not modified. · 400 ProblemDetail · 404 ProblemDetail · 500 ProblemDetail
  - tested by: `OwnerRestControllerV1Tests#testGetOwnerNotFound()`, `OwnerRestControllerV1Tests#testGetOwnerSuccess()`, `OwnerRestControllerV1Tests#testUpdateOwnerSuccess()`, `OwnerRestControllerV1Tests#testUpdateOwnerSuccessNoBodyId()`

## Existing tests

### OwnerRestControllerV1Tests — src/test/java/org/springframework/samples/petclinic/rest/controller/OwnerRestControllerV1Tests.java

- why: 4 test methods call GET /owners/{ownerId}, served by the target; tests OwnerRestControllerV1 (by name)
- annotations: @SpringBootTest @ContextConfiguration @WebAppConfiguration
- fields: @Autowired OwnerRestControllerV1 ownerRestControllerV1; @Autowired OwnerMapper ownerMapper; @Autowired PetMapper petMapper; @Autowired VisitMapper visitMapper; @MockitoBean ClinicService clinicService; MockMvc mockMvc; List<OwnerDto> owners; List<PetDto> pets; List<VisitDto> visits
- mocks: ClinicService (@MockitoBean)
- setup `OwnerRestControllerV1Tests#initOwners()`:
  ```java
  @BeforeEach
  void initOwners() {
      this.mockMvc = MockMvcBuilders.standaloneSetup(ownerRestControllerV1)
          .setControllerAdvice(new ExceptionControllerAdvice())
          .build();
      owners = new ArrayList<>();

      OwnerDto ownerWithPet = new OwnerDto();
      owners.add(ownerWithPet.id(1).firstName("George").lastName("Franklin").address("110 W. Liberty St.").city("Madison").telephone("6085551023").addPetsItem(getTestPetWithIdAndName(1, "Rosy")));
      OwnerDto owner = new OwnerDto();
      owners.add(owner.id(2).firstName("Betty").lastName("Davis").address("638 Cardinal Ave.").city("Sun Prairie").telephone("6085551749"));
      owner = new OwnerDto();
      owners.add(owner.id(3).firstName("Eduardo").lastName("Rodriquez").address("2693 Commerce St.").city("McFarland").telephone("6085558763"));
      owner = new OwnerDto();
      owners.add(owner.id(4).firstName("Harold").lastName("Davis").address("563 Friendly St.").city("Windsor").telephone("6085553198"));

      PetTypeDto petType = new PetTypeDto();
      petType.id(2)
          .name("dog");

      pets = new ArrayList<>();
      PetDto pet = new PetDto();
      pets.add(pet.id(3)
          .name("Rosy")
          .birthDate(LocalDate.now())
          .type(petType));

      pet = new PetDto();
      pets.add(pet.id(4)
          .name("Jewel")
          .birthDate(LocalDate.now())
          .type(petType));

      visits = new ArrayList<>();
      VisitDto visit = new VisitDto();
      visit.setId(2);
      visit.setPetId(pet.getId());
      visit.setDate(LocalDate.now());
      visit.setDescription("rabies shot");
      visits.add(visit);

      visit = new VisitDto();
      visit.setId(3);
      visit.setPetId(pet.getId());
      visit.setDate(LocalDate.now());
      visit.setDescription("neutered");
      visits.add(visit);
  }
  ```
- `OwnerRestControllerV1Tests#testGetOwnerSuccess()` (call GET /owners/{ownerId}; src/test/java/org/springframework/samples/petclinic/rest/controller/OwnerRestControllerV1Tests.java:153-163):
  ```java
  @Test
  @WithMockUser(roles = "OWNER_ADMIN")
  void testGetOwnerSuccess() throws Exception {
      given(this.clinicService.findOwnerById(1)).willReturn(ownerMapper.toOwner(owners.get(0)));
      this.mockMvc.perform(get("/api/owners/1")
              .accept(MediaType.APPLICATION_JSON_VALUE))
          .andExpect(status().isOk())
          .andExpect(content().contentType("application/json"))
          .andExpect(jsonPath("$.id").value(1))
          .andExpect(jsonPath("$.firstName").value("George"));
  }
  ```
- `OwnerRestControllerV1Tests#testGetOwnerNotFound()` (call GET /owners/{ownerId}; src/test/java/org/springframework/samples/petclinic/rest/controller/OwnerRestControllerV1Tests.java:165-172):
  ```java
  @Test
  @WithMockUser(roles = "OWNER_ADMIN")
  void testGetOwnerNotFound() throws Exception {
      given(this.clinicService.findOwnerById(2)).willReturn(null);
      this.mockMvc.perform(get("/api/owners/2")
              .accept(MediaType.APPLICATION_JSON))
          .andExpect(status().isNotFound());
  }
  ```

## Implementations

- ClinicService: ClinicServiceImpl

## Conventions

- seen in OwnerRestControllerV1Tests: JUnit 5 (Jupiter) (30), MockMvc (30), Spring Security test (@WithMockUser) (29), BDDMockito given(...).willReturn(...) (20), Jackson ObjectMapper for JSON bodies (20), @SpringBootTest (1), Spring test context (@ContextConfiguration, @SpringJUnitConfig...) (1), @MockitoBean mocks in the Spring context (1)
- test classes are named *Tests (*Tests: 20, *Test: 1)
- suggested test file: `src/test/java/org/springframework/samples/petclinic/rest/controller/OwnerRestControllerV1Tests.java` — OwnerRestControllerV1Tests already tests OwnerRestControllerV1: add the new tests to it

## Related docs

### REST version of Spring PetClinic Sample Application (spring-framework-petclinic extension) > 📌 API Endpoints Overview — readme.md:47-93 (score 0.75)

```markdown
## 📌 API Endpoints Overview

| **Method** | **Endpoint** | **Description** |
|-----------|------------|----------------|
| **Owners** |  |  |
| **GET** | `/api/owners` | Retrieve all pet owners |
| **GET** | `/api/owners/{ownerId}` | Get a pet owner by ID |
| **POST** | `/api/owners` | Add a new pet owner |
| **PUT** | `/api/owners/{ownerId}` | Update an owner’s details |
| **DELETE** | `/api/owners/{ownerId}` | Delete an owner |
| **GET** | `/api/owners/{ownerId}/pets/{petId}` | Get a pet by ID (owner’s pet) |
| **PUT** | `/api/owners/{ownerId}/pets/{petId}` | Update pet details (owner’s pet) |
| **POST** | `/api/owners/{ownerId}/pets` | Add a new pet to an owner |
| **POST** | `/api/owners/{ownerId}/pets/{petId}/visits` | Add a vet visit for a pet |
| **Pets** |  |  |
| **GET** | `/api/pets` | Retrieve all pets |
| **GET** | `/api/pets/{petId}` | Get a pet by ID |
| **PUT** | `/api/pets/{petId}` | Update pet details |
| **DELETE** | `/api/pets/{petId}` | Delete a pet |
| **Vets** |  |  |
| **GET** | `/api/vets` | Retrieve all veterinarians |
| **GET** | `/api/vets/{vetId}` | Get a vet by ID |
| **POST** | `/api/vets` | Add a new vet |
... (1153 more characters: get_node or `tcg show` has them)
```

### PetClinic API - Postman Non-Regression Tests > Notes — src/test/postman/README.md:87-95 (score 0.68)

```markdown
## Notes

- `ownerId` and `petId` are kept in the environment file to aid debugging in Postman GUI.
- `--suppress-exit-code` ensures the HTML report is generated even if assertions fail.
- Newman is invoked via `npx` to avoid global installation.

---

Contributions welcome! Feel free to submit pull requests to add new scenarios or enhance the script/reporting.
```

### REST version of Spring PetClinic Sample Application (spring-framework-petclinic extension) > 📖 OpenAPI REST API Documentation — readme.md:38-40 (score 0.64)

```markdown
## 📖 OpenAPI REST API Documentation
This project provides a RESTful API for managing a veterinary clinic's **owners, pets, veterinarians, visits, and specialties**.
```

## Notes

- OwnerDto is generated at build time from schema Owner (src/main/resources/openapi.yml): its fields are the schema's properties
