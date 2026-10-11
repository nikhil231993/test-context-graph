/*
 * Made-up sample code for the parser tests.
 */
package org.example.library.web;

import java.util.List;

import org.example.library.model.Loan;
import org.example.library.service.LoanService;
import org.example.library.web.dto.LoanDto;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.json.JsonMapper;

/**
 * REST endpoints for loans.
 *
 * @author Someone
 */

@RestController
@CrossOrigin(exposedHeaders = "errors, content-type")
@RequestMapping("api")
public class LoanController {

    private final LoanService loanService;

    private final LoanMapper loanMapper;

    public LoanController(LoanService loanService,
                          LoanMapper loanMapper) {
        this.loanService = loanService;
        this.loanMapper = loanMapper;
    }

    /**
     * One loan by id.
     */
    @PreAuthorize("hasRole(@roles.LIBRARIAN)")
    @GetMapping(value = "/loans/{loanId}", produces = {"application/json", "application/xml"})
    public ResponseEntity<LoanDto> getLoan(@PathVariable("loanId") Integer loanId) {
        Loan loan = this.loanService.findLoan(loanId);
        if (loan == null) {
            return new ResponseEntity<>(HttpStatus.NOT_FOUND);
        }
        return new ResponseEntity<>(loanMapper.toDto(loan), HttpStatus.OK);
    }

    @GetMapping("/loans")
    public List<LoanDto> listLoans(@RequestParam(name = "member", required = false) String member) {
        return loanMapper.toDtos(loanService.findLoans(member));
    }
}
