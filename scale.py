import serial

ser = serial.Serial(
    port='/dev/ttyUSB0',
    baudrate=9600,
    bytesize=8,
    parity='N',
    stopbits=1,
    timeout=1
)

print("Reading scale...")

while True:
    line = ser.readline().decode('utf-8', errors='ignore').strip()
    if line:
        print("RAW:", line)
