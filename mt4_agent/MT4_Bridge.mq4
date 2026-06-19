//+------------------------------------------------------------------+
//|  MT4_Bridge.mq4                                                  |
//|  File-based bridge between this MT4 terminal and mt4_worker.py.  |
//|  Attach to any one chart and leave it running.                   |
//+------------------------------------------------------------------+
#property strict
#property version "1.0"

input int CheckIntervalMs = 500;
input int MagicNumber     = 777000;
input int SlippagePoints  = 3;

string CommandsDir  = "mt4_bridge\\commands\\";
string ResultsDir   = "mt4_bridge\\results\\";
string HeartbeatFile = "mt4_bridge\\heartbeat.txt";

int OnInit() {
   EventSetMillisecondTimer(CheckIntervalMs);
   Print("MT4_Bridge started — watching ", CommandsDir);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
}

void OnTimer() {
   WriteHeartbeat();
   ProcessCommands();
}

void WriteHeartbeat() {
   int h = FileOpen(HeartbeatFile, FILE_WRITE | FILE_TXT);
   if (h != INVALID_HANDLE) {
      FileWriteString(h, TimeToString(TimeLocal(), TIME_DATE | TIME_SECONDS));
      FileClose(h);
   }
}

void ProcessCommands() {
   string filename;
   long search = FileFindFirst(CommandsDir + "*.cmd", filename);
   if (search == INVALID_HANDLE) return;
   do {
      ProcessOneCommand(filename);
   } while (FileFindNext(search, filename));
   FileFindClose(search);
}

void ProcessOneCommand(string filename) {
   string fullPath = CommandsDir + filename;
   int h = FileOpen(fullPath, FILE_READ | FILE_TXT);
   if (h == INVALID_HANDLE) return;

   string content = "";
   while (!FileIsEnding(h)) {
      content += FileReadString(h) + "\n";
   }
   FileClose(h);

   string lines[];
   int n = StringSplit(content, '\n', lines);

   string cmd = "", tradeId = "", symbol = "", direction = "long";
   double volume = 0.01, sl = 0, tp = 0;
   long ticket = 0;

   for (int i = 0; i < n; i++) {
      int eq = StringFind(lines[i], "=");
      if (eq < 0) continue;
      string key = StringSubstr(lines[i], 0, eq);
      string val = StringSubstr(lines[i], eq + 1);
      if (key == "command")    cmd = val;
      else if (key == "trade_id") tradeId = val;
      else if (key == "symbol")   symbol = val;
      else if (key == "direction") direction = val;
      else if (key == "volume")   volume = StrToDouble(val);
      else if (key == "sl")       sl = StrToDouble(val);
      else if (key == "tp")       tp = StrToDouble(val);
      else if (key == "ticket")   ticket = StrToInteger(val);
   }

   string result;
   if (cmd == "open")       result = DoOpen(tradeId, symbol, direction, volume, sl, tp);
   else if (cmd == "close") result = DoClose(tradeId, symbol, direction, volume, ticket);
   else                     result = "command=" + cmd + "\nerror=unknown command\n";

   string resultName = StringSubstr(filename, 0, StringLen(filename) - 4) + ".result";
   int rh = FileOpen(ResultsDir + resultName, FILE_WRITE | FILE_TXT);
   if (rh != INVALID_HANDLE) {
      FileWriteString(rh, "trade_id=" + tradeId + "\n" + result);
      FileClose(rh);
   }

   FileDelete(fullPath);
}

string DoOpen(string tradeId, string symbol, string direction, double volume, double sl, double tp) {
   int type = (direction == "long") ? OP_BUY : OP_SELL;
   double price = (direction == "long") ? MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);

   if (price <= 0) {
      return "command=open\nerror=Symbol " + symbol + " ikke fundet eller ingen pris\n";
   }

   color arrowColor = (direction == "long") ? clrBlue : clrRed;
   int ticket = OrderSend(symbol, type, volume, price, SlippagePoints, sl, tp,
                          "auto_" + StringSubstr(tradeId, 0, 8), MagicNumber, 0, arrowColor);

   if (ticket < 0) {
      return "command=open\nerror=MT4 error " + IntegerToString(GetLastError()) + "\n";
   }

   double openPrice = price;
   if (OrderSelect(ticket, SELECT_BY_TICKET)) {
      openPrice = OrderOpenPrice();
   }

   return "command=open\nticket=" + IntegerToString(ticket) +
          "\nprice=" + DoubleToString(openPrice, 5) +
          "\nvolume=" + DoubleToString(volume, 2) +
          "\nsymbol=" + symbol +
          "\ndirection=" + direction +
          "\nerror=\n";
}

string DoClose(string tradeId, string symbol, string direction, double volume, long ticket) {
   if (!OrderSelect((int)ticket, SELECT_BY_TICKET)) {
      return "command=close\nerror=Ordre ikke fundet (ticket " + IntegerToString((int)ticket) + ")\n";
   }

   double closePrice = (direction == "long") ? MarketInfo(symbol, MODE_BID) : MarketInfo(symbol, MODE_ASK);
   bool ok = OrderClose((int)ticket, volume, closePrice, SlippagePoints, clrYellow);

   if (!ok) {
      return "command=close\nerror=MT4 error " + IntegerToString(GetLastError()) + "\n";
   }

   return "command=close\nclosed=true\nprice=" + DoubleToString(closePrice, 5) +
          "\nticket=" + IntegerToString((int)ticket) + "\nerror=\n";
}
