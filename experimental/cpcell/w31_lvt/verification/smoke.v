module logic_batch_smoke(input A,B,C,D,E,output ynand,ynor,yaoi,yoai);
 gt2_6t_nand4_w31_lvt n0(.A(A),.B(B),.C(C),.D(D),.Y(ynand));
 gt2_6t_nor4_w31_lvt n1(.A(A),.B(B),.C(C),.D(D),.Y(ynor));
 gt2_6t_aoi221_w31_lvt n2(.A1(A),.A2(B),.B1(C),.B2(D),.C(E),.Y(yaoi));
 gt2_6t_oai221_w31_lvt n3(.A1(A),.A2(B),.B1(C),.B2(D),.C(E),.Y(yoai));
endmodule
