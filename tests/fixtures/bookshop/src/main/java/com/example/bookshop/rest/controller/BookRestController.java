package com.example.bookshop.rest.controller;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.example.bookshop.mapper.BookMapper;
import com.example.bookshop.model.Book;
import com.example.bookshop.rest.api.BooksApi;
import com.example.bookshop.rest.dto.BookDto;
import com.example.bookshop.service.BookService;

@RestController
@RequestMapping("api")
public class BookRestController implements BooksApi {

    private final BookService bookService;

    private final BookMapper bookMapper;

    public BookRestController(BookService bookService, BookMapper bookMapper) {
        this.bookService = bookService;
        this.bookMapper = bookMapper;
    }

    @Override
    public ResponseEntity<BookDto> getBook(Integer bookId) {
        Book book = bookService.findBook(bookId);
        if (book == null) {
            return new ResponseEntity<>(HttpStatus.NOT_FOUND);
        }
        return new ResponseEntity<>(bookMapper.toBookDto(book), HttpStatus.OK);
    }

    @Override
    public ResponseEntity<List<BookDto>> listBooks() {
        return new ResponseEntity<>(bookMapper.toBookDtos(bookService.findBooks()), HttpStatus.OK);
    }
}
