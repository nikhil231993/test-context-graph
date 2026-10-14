package com.example.bookshop.mapper;

import java.util.Collection;
import java.util.List;

import org.mapstruct.Mapper;

import com.example.bookshop.model.Book;
import com.example.bookshop.rest.dto.BookDto;

@Mapper
public interface BookMapper {

    BookDto toBookDto(Book book);

    List<BookDto> toBookDtos(Collection<Book> books);
}
