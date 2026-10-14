# Bookshop

A made-up Spring application used as a test fixture.

## Finding books

`BookService` finds books through a `BookRepository`. The REST API serves them at
`/api/books/{bookId}`.

## Profiles

The `jdbc` profile selects `JdbcBookRepository`.
