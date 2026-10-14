# Test context: ClinicServiceImpl#findOwnerById(int)

method `method:org.springframework.samples.petclinic.service.ClinicServiceImpl#findOwnerById(int)` · service · src/main/java/org/springframework/samples/petclinic/service/ClinicServiceImpl.java:200-204

## Target

`public Owner findOwnerById(int id) throws DataAccessException`
```java
@Override
@Transactional(readOnly = true)
public Owner findOwnerById(int id) throws DataAccessException {
    return findEntityById(() -> ownerRepository.findById(id));
}
```

## Enclosing class: ClinicServiceImpl (service)

@Service · src/main/java/org/springframework/samples/petclinic/service/ClinicServiceImpl.java:40-266
```java
/**
 * Mostly used as a facade for all Petclinic controllers
 * Also a placeholder for @Transactional and @Cacheable annotations
 *
 * @author Michael Isvy
 * @author Vitaliy Fedoriv
 */
@Service
public class ClinicServiceImpl implements ClinicService {
    private final PetRepository petRepository;
    private final VetRepository vetRepository;
    private final OwnerRepository ownerRepository;
    private final VisitRepository visitRepository;
    private final SpecialtyRepository specialtyRepository;
    private final PetTypeRepository petTypeRepository;

    public ClinicServiceImpl(PetRepository petRepository, VetRepository vetRepository, OwnerRepository ownerRepository, VisitRepository visitRepository, SpecialtyRepository specialtyRepository, PetTypeRepository petTypeRepository);
    // ... 30 more methods: get_node or `tcg show type:org.springframework.samples.petclinic.service.ClinicServiceImpl`
    @Override @Transactional(readOnly = true) public Owner findOwnerById(int id) throws DataAccessException;
    private <T> T findEntityById(Supplier<T> supplier);
}
```

## Dependencies to mock

- **OwnerRepository** (interface, repository; field `ownerRepository` via constructor)
  - calls `Owner findById(int id) throws DataAccessException`
- **PetRepository** (interface, repository; field `petRepository` via constructor) — not called by the target; needed to build the class
- **PetTypeRepository** (interface, repository; field `petTypeRepository` via constructor) — not called by the target; needed to build the class
- **SpecialtyRepository** (interface, repository; field `specialtyRepository` via constructor) — not called by the target; needed to build the class
- **VetRepository** (interface, repository; field `vetRepository` via constructor) — not called by the target; needed to build the class
- **VisitRepository** (interface, repository; field `visitRepository` via constructor) — not called by the target; needed to build the class

## Other calls

- `ClinicServiceImpl#findEntityById(Supplier)` — same class, resolved by same_type: `private <T> T findEntityById(Supplier<T> supplier)`
  ```java
  private <T> T findEntityById(Supplier<T> supplier) {
      try {
          return supplier.get();
      } catch (ObjectRetrievalFailureException | EmptyResultDataAccessException e) {
          // Just ignore not found exceptions for Jdbc/Jpa realization
          return null;
      }
  }
  ```

## Data types

- **Owner** (entity class); return of findOwnerById; return of OwnerRepository#findById(int)
  - fields: address: String @Column @NotEmpty, city: String @Column @NotEmpty, telephone: String @Column @NotEmpty @Digits @Pattern, pets: Set<Pet> @OneToMany, firstName: String @Column @NotEmpty (from Person), lastName: String @Column @NotEmpty (from Person), id: Integer @Id @GeneratedValue (from BaseEntity)

## Existing tests

### AbstractClinicServiceTests (abstract) — src/test/java/org/springframework/samples/petclinic/service/clinicService/AbstractClinicServiceTests.java

- why: 4 test methods call ClinicService#findOwnerById(int), which the target implements; tests ClinicService (by name), a supertype of ClinicServiceImpl
- fields: @Autowired ClinicService clinicService
- subclass **ClinicServiceH2JdbcTests** [active profiles: h2, jdbc] — src/test/java/org/springframework/samples/petclinic/service/clinicService/ClinicServiceH2JdbcTests.java
- subclass **ClinicServiceHsqlJdbcTests** [active profiles: hsqldb, jdbc] — src/test/java/org/springframework/samples/petclinic/service/clinicService/ClinicServiceHsqlJdbcTests.java
- subclass **ClinicServiceJpaTests** [active profiles: jpa, hsqldb] — src/test/java/org/springframework/samples/petclinic/service/clinicService/ClinicServiceJpaTests.java
- subclass **ClinicServiceSpringDataJpaTests** [active profiles: spring-data-jpa, hsqldb] — src/test/java/org/springframework/samples/petclinic/service/clinicService/ClinicServiceSpringDataJpaTests.java
- `AbstractClinicServiceTests#shouldFindSingleOwnerWithPet()` (call ClinicService#findOwnerById(int); src/test/java/org/springframework/samples/petclinic/service/clinicService/AbstractClinicServiceTests.java:71-78):
  ```java
  @Test
  void shouldFindSingleOwnerWithPet() {
      Owner owner = this.clinicService.findOwnerById(1);
      assertThat(owner.getLastName()).startsWith("Franklin");
      assertThat(owner.getPets().size()).isEqualTo(1);
      assertThat(owner.getPets().get(0).getType()).isNotNull();
      assertThat(owner.getPets().get(0).getType().getName()).isEqualTo("cat");
  }
  ```
- `AbstractClinicServiceTests#shouldUpdateOwner()` (call ClinicService#findOwnerById(int); src/test/java/org/springframework/samples/petclinic/service/clinicService/AbstractClinicServiceTests.java:99-112):
  ```java
  @Test
  @Transactional
  void shouldUpdateOwner() {
      Owner owner = this.clinicService.findOwnerById(1);
      String oldLastName = owner.getLastName();
      String newLastName = oldLastName + "X";

      owner.setLastName(newLastName);
      this.clinicService.saveOwner(owner);

      // retrieving new name from database
      owner = this.clinicService.findOwnerById(1);
      assertThat(owner.getLastName()).isEqualTo(newLastName);
  }
  ```

## Implementations

- OwnerRepository: SpringDataOwnerRepository [profiles: spring-data-jpa], JdbcOwnerRepositoryImpl [profiles: jdbc], JpaOwnerRepositoryImpl [profiles: jpa]

## Conventions

- seen in AbstractClinicServiceTests, ClinicServiceH2JdbcTests, ClinicServiceHsqlJdbcTests, ClinicServiceJpaTests, ClinicServiceSpringDataJpaTests: JUnit 5 (Jupiter) (36), AssertJ assertThat(...).isEqualTo(...) (36), @Transactional tests (rolled back) (20), @SpringBootTest (4), @ActiveProfiles (4), Spring test context (@ContextConfiguration, @SpringJUnitConfig...) (2)
- test classes are named *Tests (*Tests: 20, *Test: 1)
- suggested test file: `src/test/java/org/springframework/samples/petclinic/service/ClinicServiceImplTests.java` — no test class for ClinicServiceImpl yet: create ClinicServiceImplTests.java; AbstractClinicServiceTests tests what ClinicServiceImpl implements and runs under 4 subclasses (profiles): add integration tests there to cover every implementation

## Related docs

### REST version of Spring PetClinic Sample Application (spring-framework-petclinic extension) > Looking for something in particular? — readme.md:275-285 (score 0.65)

```markdown
## Looking for something in particular?

| Layer | Source |
|--|--|
| REST API controllers | [REST folder](src/main/java/org/springframework/samples/petclinic/rest) |
| Service | [ClinicServiceImpl.java](src/main/java/org/springframework/samples/petclinic/service/ClinicServiceImpl.java) |
| JDBC | [jdbc folder](src/main/java/org/springframework/samples/petclinic/repository/jdbc) |
| JPA | [jpa folder](src/main/java/org/springframework/samples/petclinic/repository/jpa) |
| Spring Data JPA | [springdatajpa folder](src/main/java/org/springframework/samples/petclinic/repository/springdatajpa) |
| Tests | [AbstractClinicServiceTests.java](src/test/java/org/springframework/samples/petclinic/service/clinicService/AbstractClinicServiceTests.java) |
```

### REST version of Spring PetClinic Sample Application (spring-framework-petclinic extension) > 📌 API Endpoints Overview — readme.md:47-93 (score 0.64)

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

### PetClinic API - Postman Non-Regression Tests > Notes — src/test/postman/README.md:87-95 (score 0.61)

```markdown
## Notes

- `ownerId` and `petId` are kept in the environment file to aid debugging in Postman GUI.
- `--suppress-exit-code` ensures the HTML report is generated even if assertions fail.
- Newman is invoked via `npx` to avoid global installation.

---

Contributions welcome! Feel free to submit pull requests to add new scenarios or enhance the script/reporting.
```
