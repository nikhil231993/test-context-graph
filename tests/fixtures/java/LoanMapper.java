package org.example.library.web;

import java.util.Collection;
import java.util.List;

import org.example.library.model.Loan;
import org.example.library.web.dto.LoanDto;
import org.mapstruct.Mapper;
import org.mapstruct.Mapping;

/**
 * Maps loans to DTOs.
 */
@Mapper(componentModel = "spring", uses = BookMapper.class)
public interface LoanMapper {

    int MAX_BATCH = 50;

    @Mapping(source = "book.id", target = "bookId")
    @Mapping(target = "notes", ignore = true)
    LoanDto toDto(Loan loan);

    List<LoanDto> toDtos(Collection<Loan> loans);

    default List<LoanDto> toDtosSorted(Collection<Loan> loans) {
        return toDtos(loans).stream().sorted().toList();
    }
}
