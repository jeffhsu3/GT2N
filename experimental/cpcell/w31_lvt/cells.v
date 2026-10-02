// Functional models. Power pins are implicit, as in synthesis Liberty.
module gt2_6t_nand4_w31_lvt (A, B, C, D, Y);
  input A, B, C, D;
  output Y;
  assign Y = !(A & B & C & D);
endmodule

module gt2_6t_nor4_w31_lvt (A, B, C, D, Y);
  input A, B, C, D;
  output Y;
  assign Y = !(A | B | C | D);
endmodule

module gt2_6t_aoi221_w31_lvt (A1, A2, B1, B2, C, Y);
  input A1, A2, B1, B2, C;
  output Y;
  assign Y = !((A1 & A2) | (B1 & B2) | C);
endmodule

module gt2_6t_oai221_w31_lvt (A1, A2, B1, B2, C, Y);
  input A1, A2, B1, B2, C;
  output Y;
  assign Y = !((A1 | A2) & (B1 | B2) & C);
endmodule
