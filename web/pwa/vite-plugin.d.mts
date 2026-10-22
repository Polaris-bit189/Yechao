declare function yechaoPwa(): {
  name: string
  apply: 'build'
  closeBundle(): Promise<void>
}

export default yechaoPwa
