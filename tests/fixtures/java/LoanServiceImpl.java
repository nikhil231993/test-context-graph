package org.example.library.service;

import java.util.List;
import java.util.function.Supplier;

import org.example.library.model.*;
import org.example.library.repository.BookRepository;
import org.example.library.repository.LoanRepository;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.orm.ObjectRetrievalFailureException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class LoanServiceImpl extends AbstractAuditedService implements LoanService {

    private final LoanRepository loanRepository;
    private final BookRepository bookRepository;
    private Clock clock;

    public LoanServiceImpl(LoanRepository loanRepository, BookRepository bookRepository) {
        super("loans");
        this.loanRepository = loanRepository;
        this.bookRepository = bookRepository;
    }

    @Override
    @Transactional(readOnly = true)
    public Loan findLoan(int loanId) {
        return findOrNull(() -> loanRepository.findById(loanId));
    }

    @Override
    @Transactional
    public void returnAll(Member member, List<Loan> loans) {
        for (Loan loan : loans) {
            loan.markReturned(this.clock.now());
        }
        var receipt = new Receipt(member);
        var batch = loanRepository.newBatch();
        batch.flush();
        loans.forEach(each -> each.archive());
        loans.removeIf((Loan l) -> l.isLost());
        loans.stream().map(Loan::getBook).forEach(this::restock);
        member.notify(receipt.summary());
        try {
            loanRepository.saveAll(loans);
        } catch (IllegalStateException e) {
            e.printStackTrace();
        }
        Book book = bookRepository.findFirst();
        book.touch();
        super.audit("returned " + loans.size() + " loans");
        LoanPolicy.check(member).apply(loans);
    }

    private void restock(Book book) {
        bookRepository.save(book);
    }

    private <T> T findOrNull(Supplier<T> supplier) {
        try {
            return supplier.get();
        } catch (ObjectRetrievalFailureException | EmptyResultDataAccessException e) {
            e.getMessage();
            return null;
        }
    }
}
