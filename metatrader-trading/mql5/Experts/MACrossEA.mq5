//+------------------------------------------------------------------+
//| MACrossEA.mq5 - Moving-average crossover Expert Advisor          |
//| Starter template. TEST ON A DEMO ACCOUNT FIRST.                  |
//+------------------------------------------------------------------+
#property copyright "metatrader-trading"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

input int    FastPeriod   = 10;     // Fast MA period
input int    SlowPeriod   = 30;     // Slow MA period
input double RiskPercent  = 1.0;    // Risk per trade (% of balance)
input int    StopLossPts  = 300;    // Stop loss (points)
input int    TakeProfitPts= 600;    // Take profit (points)
input ulong  MagicNumber  = 20260101;

CTrade trade;
int fastHandle, slowHandle;
datetime lastBar = 0;

int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   fastHandle = iMA(_Symbol, _Period, FastPeriod, 0, MODE_EMA, PRICE_CLOSE);
   slowHandle = iMA(_Symbol, _Period, SlowPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(fastHandle == INVALID_HANDLE || slowHandle == INVALID_HANDLE)
      return INIT_FAILED;
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   IndicatorRelease(fastHandle);
   IndicatorRelease(slowHandle);
}

bool HasPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(PositionSelectByTicket(t) &&
         PositionGetString(POSITION_SYMBOL) == _Symbol &&
         PositionGetInteger(POSITION_MAGIC) == (long)MagicNumber)
         return true;
   }
   return false;
}

double CalcLots()
{
   double risk   = AccountInfoDouble(ACCOUNT_BALANCE) * RiskPercent / 100.0;
   double tickV  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickS  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double slMoney= StopLossPts * _Point / tickS * tickV;
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minV   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxV   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(slMoney <= 0) return minV;
   double lots = MathFloor(risk / slMoney / step) * step;
   return MathMax(minV, MathMin(maxV, lots));
}

void OnTick()
{
   datetime bar = iTime(_Symbol, _Period, 0);
   if(bar == lastBar) return;          // act once per new bar
   lastBar = bar;
   if(HasPosition()) return;

   double f[3], s[3];
   if(CopyBuffer(fastHandle, 0, 0, 3, f) < 3) return;
   if(CopyBuffer(slowHandle, 0, 0, 3, s) < 3) return;
   // Arrays are oldest->newest: [0]=2 bars ago, [1]=last closed bar, [2]=forming bar.
   // Compare the two closed bars [0] -> [1].
   bool crossUp   = f[0] <= s[0] && f[1] > s[1];
   bool crossDown = f[0] >= s[0] && f[1] < s[1];

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double lots = CalcLots();

   if(crossUp)
      trade.Buy(lots, _Symbol, ask, ask - StopLossPts*_Point, ask + TakeProfitPts*_Point, "MA cross up");
   else if(crossDown)
      trade.Sell(lots, _Symbol, bid, bid + StopLossPts*_Point, bid - TakeProfitPts*_Point, "MA cross down");
}
