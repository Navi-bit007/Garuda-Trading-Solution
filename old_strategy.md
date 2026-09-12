// This source code is subject to the terms of the Mozilla Public License 2.0 at https://mozilla.org/MPL/2.0/
// © zarafshani20
//@version=5
indicator('Alxuse Supertrend 4EMA Buy and Sell for tutorial', overlay=true, timeframe = "")
MAprice = input(close)
MAL1 = input.int(20, step=1, title='EMA 1')
MAL2 = input.int(50, step=1, title='EMA 2')
MAL3 = input.int(100, step=1, title='EMA 3')
MAL4 = input.int(200, step=1, title='EMA 4')
switchema = input(true, title='SEMA??')
MA1 = ta.ema(MAprice, MAL1)
MA2 = ta.ema(MAprice, MAL2)
MA3 = ta.ema(MAprice, MAL3)
MA4 = ta.ema(MAprice, MAL4)
change_1 = ta.change(MA1)
MA1_col = ta.change(MA1) > 0 ? #ffed4b : change_1 < 0 ? #ffed4b83 : #ffed4b
change_2 = ta.change(MA2)
MA2_col = ta.change(MA2) > 0 ? #f32121 : change_2 < 0 ? #f321218a  : #f32121 
change_3 = ta.change(MA3)
MA3_col = ta.change(MA3) > 0 ? #1c3dfd : change_3 < 0 ? #1c3efd8a : #1c3dfd
change_4 = ta.change(MA4)
MA4_col = ta.change(MA4) > 0 ? #3aff20 : change_4 < 0 ? #3aff208c : #3aff20
EMA1 = plot(switchema ? MA1 : na, title='EMA 1', style=plot.style_linebr, linewidth=1, color=MA1_col)
EMA2 = plot(switchema ? MA2 : na, title='EMA 2', style=plot.style_linebr, linewidth=1, color=MA2_col)
EMA3 = plot(switchema ? MA3 : na, title='EMA 3', style=plot.style_linebr, linewidth=1, color=MA3_col)
EMA4 = plot(switchema ? MA4 : na, title='EMA 4', style=plot.style_linebr, linewidth=1, color=MA4_col)
Sourcebs = input(close, 'Buy&Sell Source Type')
FAP = input(5, 'Buy&Sell Fap')
FAM = input(0.5, 'Buy&Sell Fam')
FAPFAM = FAM * ta.atr(FAP)
Trailing = 0.0
iff_11 = Sourcebs > nz(Trailing[1], 0) ? Sourcebs - FAPFAM : Sourcebs + FAPFAM
iff_2 = Sourcebs < nz(Trailing[1], 0) and Sourcebs[1] < nz(Trailing[1], 0) ? math.min(nz(Trailing[1], 0), Sourcebs + FAPFAM) : iff_11
Trailing := Sourcebs > nz(Trailing[1], 0) and Sourcebs[1] > nz(Trailing[1], 0) ? math.max(nz(Trailing[1], 0), Sourcebs - FAPFAM) : iff_2
SAP = input(21, 'Buy&Sell Sap')
SAM = input.float(7, 'Buy&Sell Sam')
SAPSAM = SAM * ta.atr(SAP)
Trailing1 = 0.0
iff_3 = Sourcebs > nz(Trailing1[1], 0) ? Sourcebs - SAPSAM : Sourcebs + SAPSAM
iff_4 = Sourcebs < nz(Trailing1[1], 0) and Sourcebs[1] < nz(Trailing1[1], 0) ? math.min(nz(Trailing1[1], 0), Sourcebs + SAPSAM) : iff_3
Trailing1 := Sourcebs > nz(Trailing1[1], 0) and Sourcebs[1] > nz(Trailing1[1], 0) ? math.max(nz(Trailing1[1], 0), Sourcebs - SAPSAM) : iff_4
Buy = ta.crossover(Trailing, Trailing1)
Sell = ta.crossunder(Trailing, Trailing1)
plotshape(Buy, 'BUY', shape.labelup, location.belowbar, color.new(color.green, 0), text='BUY', textcolor=color.new(color.black, 0))
plotshape(Sell, 'SELL', shape.labeldown, location.abovebar, color.new(color.red, 0), text='SELL', textcolor=color.new(color.black, 0))
alertcondition(Buy, 'Buy Superrend Signal', 'Buy Signal')
alertcondition(Sell, 'Sell Superrend Signal', 'Sell Signal')

//x = ta.crossover(MA2, MA3)  and MA1 < MA4
x1 = MA1 < MA2 and  MA2 < MA3 and MA3 < MA4  and ta.crossunder(MA3, MA4)
x2 = MA1 < MA2 and  MA2 < MA3 and MA3 < MA4  and ta.crossunder(MA2, MA3)
x3 = MA1 < MA2 and  MA2 < MA3 and MA3 < MA4  and ta.crossunder(MA1, MA2)
y1 = MA4 < MA3 and  MA3 < MA2 and MA2 < MA1 and ta.crossover(MA3, MA4)
y2 = MA4 < MA3 and  MA3 < MA2 and MA2 < MA1 and ta.crossover(MA2, MA3)
y3 = MA4 < MA3 and  MA3 < MA2 and MA2 < MA1 and ta.crossover(MA1, MA2)
plotshape(x1, title="X1", color=color.red, location=location.abovebar , style = shape.triangledown, size = size.small)
plotshape(x2, title="X12", color=color.red, location=location.abovebar ,  style = shape.triangledown, size = size.small)
plotshape(x3, title="X3", color=color.red, location=location.abovebar , style = shape.triangledown, size = size.small)
plotshape(y1, title="y1", color=color.green, location=location.belowbar , style = shape.triangleup, size = size.small)
plotshape(y2, title="y2", color=color.green, location=location.belowbar , style = shape.triangleup, size = size.small)
plotshape(y3, title="y3", color=color.green, location=location.belowbar , style = shape.triangleup, size = size.small)

alertcondition(x1 or x2 or x3, 'Sell 4EMA Signal', 'Sell Signal 4EMA (Red Triangle)')
alertcondition(y1 or y2 or y3, 'Buy 4EMA Signal', 'Buy Signal 4EMA (Green Triangle)')